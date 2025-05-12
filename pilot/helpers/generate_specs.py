import os
from pathlib import Path
import subprocess
from tabulate import tabulate
from helpers.specs_database import initialize_db, save_processed_file, get_processed_file
from jinja2 import Template, Environment, FileSystemLoader
from openai import AzureOpenAI, OpenAI
import tiktoken
from typing import List

tokenizer = tiktoken.get_encoding("cl100k_base")
get_from_database = False


def get_tokens_in_messages(messages: List[str]) -> int:
    tokenized_messages = [tokenizer.encode(message['content']) for message in messages]
    return sum(len(tokens) for tokens in tokenized_messages)

ALLOWED_EXTENSIONS = {'.html', '.css', '.js', '.py', '.java', '.c', '.cpp', '.txt', '.md', '.json', '.xml', '.yaml',
                      '.pug', '.svelte', '.ts', '.tsx', '.less', '.scss', '.sh', '.feature'}

IGNORED_FOLDERS = {'test', '.git', 'build', 'resources', '.github', '.idea', '__pycache__', 'node_modules', 'dist',
                   'integ-test', 'terraform', '.pytest_cache', 'venv', 'migrations', 'public', 'coverage', 'tmp'}

# API settings - make sure to replace 'your_api_key' with your actual OpenAI API key

def call_openai_gpt(prompt, context, model="gpt-35-turbo"):
    response = ''
    API_URL = os.getenv("AZURE_ENDPOINT")
    API_VERSION = os.getenv("AZURE_API_VERSION")
    API_KEY = os.getenv("AZURE_API_KEY")

    tokens = get_tokens_in_messages([{
        'role': 'user',
        'content': prompt
    }])

    # print(f"API_URL: {API_URL}")
    # print(f"API_VERSION: {API_VERSION}")
    # print(f"API_KEY: {API_KEY}")
    # print('model:', model)
    print(f"File {context['file_path'] if 'file_path' in context else context['folder_path'] if 'folder_path' in context else 'FINAL'} has {tokens} tokens")

    if not API_URL or not API_VERSION or not API_KEY:
        client = OpenAI(
            # This is the default and can be omitted
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
        model = "gpt-3.5-turbo"
    else:
        client = AzureOpenAI(
            api_version=API_VERSION,
            azure_endpoint=API_URL,
            api_key=API_KEY,
        )

    if tokens > 14000:
        model = "gpt-4-turbo"
    if tokens > 124000:
        print("Too many tokens, skipping")
        print('-'*80)
        print(prompt)
        print('-'*80)
        return 'File too big so cannot be processed.'

    stream = client.chat.completions.create(
        model=model,
        messages=[{
            'role': 'system',
            'content': "You are a technical writer who is proficient in making project specifications. Your usual job is to create specifications for codebases that don't have any specs. You start with spec creation by looking at each file one by one individually and writing down what features this file might support. Then, when you have specs from all files, you go through the codebase once again and improve each file by knowing the context of other files as well. You start each of the specs by stating the file name and then writing the specs below. Here is an example:\n\nserver.js\nUsed for starting the server..."
        }, {
            'role': 'user',
            'content': prompt
        }],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices == []:
            continue
        c = chunk.choices[0].delta.content or ""
        response += c
        print(c, end="")

    print('\n\n')
    return response


def render_and_call_openai(template_path, context, model="gpt-35-turbo"):
    """
    Reads a Jinja template from a relative path, renders the template with the provided context,
    and calls the OpenAI API with the rendered prompt.

    Args:
    template_path (str): The relative path to the Jinja template file.
    context (dict): A dictionary containing the variables to fill in the template.
    """
    # Set up the Jinja environment to load templates from the directory containing the template
    template_dir, template_filename = template_path.rsplit('/', 1)
    env = Environment(loader=FileSystemLoader(template_dir or './'))
    template = env.get_template(template_filename)

    # Render the template with the provided context
    filled_prompt = template.render(context)
    # print("Generated Prompt:", filled_prompt)  # Optional: for debugging/verification

    # Call OpenAI with the rendered prompt
    return call_openai_gpt(filled_prompt, context, model)

def list_extensions(directory):
    extensions = set()
    for root, _, files in os.walk(directory):
        for file in files:
            ext = Path(file).suffix
            extensions.add(ext)
    return sorted(list(extensions))  # Sort for better display


def count_lines(directory):
    # Path to the Bash script within the 'resources' folder
    script_path = os.path.join('resources', 'count_number_of_lines_in_a_dir_sorted.sh')

    # Make sure the script is executable
    os.chmod(script_path, 0o755)

    # Running the Bash script
    try:
        result = subprocess.run([script_path, directory], capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"An error occurred: {e.stderr}"


def traverse_tree_depth_first(node, action):
    """
    Traverses the directory tree starting from the deepest level upwards,
    applying an action to each node.

    Args:
    node (dict): A dictionary representing a node in the directory tree.
    action (function): A function that takes a node as an argument and performs an operation.
    """
    # If the node is a folder and has children, dive deeper first
    if (node['type'] == 'folder' or node['type'] == 'root') and 'children' in node:
        # for i in range(len(node['children'])):
        #     node['children'][i]['description'] = traverse_tree_depth_first(node['children'][i], action)
        for child in node['children']:
            child['description'] = traverse_tree_depth_first(child, action)
    # Apply the action to the current node
    return action(node)

full_project_specs = ''
def update_full_project_specs(node):
    global full_project_specs
    full_project_specs += '\n\n## ' + node['description']

def create_project_specs(directory):
    global full_project_specs
    printable_structure, project_structure = generate_directory_structure(directory)

    # pass #1: standalone descriptions
    project_structure['description'] = traverse_tree_depth_first(project_structure,
                                                                 lambda node: process_file_and_save(node, printable_structure, 'standalone'))

    project_structure['type'] = 'root'
    project_specs_summary = get_file_description(project_structure, project_structure)

    traverse_tree_depth_first(project_structure, update_full_project_specs)

    save_specs(project_specs_summary, full_project_specs)



def save_specs(summary, full_specs):
    file_path = 'output/full_project_specs.md'
    with open(file_path, 'w') as file:
        file.write(full_specs)

    file_path = 'output/project_spec_summary.md'
    with open(file_path, 'w') as file:
        file.write(summary)


def process_file_and_save(node, project_structure, processing_type):
    if get_from_database:
        return getattr(get_processed_file(node['path'], processing_type), processing_type + '_description')
    description = get_file_description(node, project_structure)
    save_processed_file(node['name'],
                        node['path'],
                        node['type'],
                        standalone_description=description if processing_type == 'standalone' else None,
                        contextual_description=description if processing_type == 'contextual' else None)
    return description




def get_file_description(node, project_structure):
    if node['type'] == 'file':
        with open(node['path'], 'r') as file:
            content = file.read()
            if content == '':
                return 'Empty file'
            return render_and_call_openai('prompts/spec_autogen/get_file_description.prompt', {
                'file_path': node['path'],
                'project_structure': project_structure,
                'file_content': content})
    elif node['type'] == 'folder':
        return render_and_call_openai('prompts/spec_autogen/get_folder_description.prompt', {
            'folder_path': node['path'],
            'project_structure': project_structure,
            'children': node['children']})
    elif node['type'] == 'root':
        return render_and_call_openai('prompts/spec_autogen/get_project_specs.prompt', {
            'project_path': node['path'],
            'project_structure': project_structure,
            'children': node['children'],
            'node': node,
            'is_root': True,
        }, 'gpt-4-turbo')


def generate_directory_structure(directory, prefix=''):
    """
    Returns both a printable string and a structured dictionary of the directory structure
    with files filtered by specific extensions and skips directories specified in IGNORED_FOLDERS.

    Args:
    directory (str): The directory path to analyze.
    prefix (str): A prefix used to indicate the level of nesting.

    Returns:
    tuple: A string representing the printable directory structure,
           and a dictionary representing the structured directory tree.
    """
    structure_str = ""
    if prefix == '':  # This is the top level directory
        structure_str += directory + "\n"

    tree_dict = {"name": Path(directory).name, "path": directory, "type": "folder", "children": []}
    entries = list(os.scandir(directory))
    subdirs = [entry for entry in entries if entry.is_dir() and entry.name not in IGNORED_FOLDERS]
    files = [entry for entry in entries if entry.is_file() and Path(entry.name).suffix in ALLOWED_EXTENSIONS]

    # Process files and add to tree and string
    for file in sorted(files, key=lambda e: e.name):
        structure_str += f"{prefix}├── {file.name}\n"
        tree_dict["children"].append({"name": file.name, "path": file.path, "type": "file"})

    # Recursively process subdirectories
    for i, subdir in enumerate(sorted(subdirs, key=lambda e: e.name)):
        if i == len(subdirs) - 1:
            new_prefix = f"{prefix}    "  # Adjust prefix for the last element
        else:
            new_prefix = f"{prefix}│   "

        structure_str += f"{prefix}├── {subdir.name}\n"
        subdir_str, subdir_dict = generate_directory_structure(subdir.path, new_prefix)
        structure_str += subdir_str
        tree_dict["children"].append(subdir_dict)

    return structure_str, tree_dict

def generate_specs():
    # directory = ""
    directory = input("Please paste the directory path: ")

    if not os.path.isdir(directory):
        print("This is not a valid directory.")
        return

    initialize_db()

    choice = input("Choose an option:\n"
                   "a) List all file extensions\n"
                   "b) Count the number of lines of code\n"
                   "c) Create the project specs*\n"
                   "d) Print the folder structure\n"
                   "Enter your choice (a, b, c, or d): ")

    if choice.lower() == 'a':
        extensions = list_extensions(directory)
        if len(extensions) % 3 != 0:
            extensions = extensions + ["-"] * (len(extensions) % 3)
        # Organize extensions into sublists of three
        it = iter(extensions)
        grouped_extensions = list(zip(*[it] * 3))
        # Handle any remaining extensions if total number isn't a multiple of 3
        # if len(extensions) % 3 != 0:
        #     grouped_extensions.append(tuple(it))
        # Print the table with three columns
        print(tabulate(grouped_extensions, headers=["Extension 1", "Extension 2", "Extension 3"], tablefmt="grid"))
    elif choice.lower() == 'b':
        lines = count_lines(directory)
        print(f"Total number of lines of code in all files: {lines}")
    elif choice.lower() == 'c':
        create_project_specs(directory)
        print("Custom script has been run on each file.")
    elif choice.lower() == 'd':
        printable_project_structure, project_structure = generate_directory_structure(directory)
        print(printable_project_structure)
    else:
        print("Invalid option. Please select 'a', 'b', or 'c'.")

