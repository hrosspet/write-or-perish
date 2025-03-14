#!/usr/bin/env python3
import subprocess
import os
import sys

# Files to always exclude by filename.
EXCLUDE_FILES = {".env", ".gitignore", ".gitattributes", ".editorconfig", ".gitmodules", "package-lock.json", "package.json", "generate_prompt.py"}

# Define a whitelist of source-code file extensions (in lowercase).
# Only files with these extensions will be included.
SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",        # Python and JS/TS files
    ".html", ".css", ".scss",                    # Web files
    ".c", ".h", ".cpp", ".hpp", ".cc",            # C/C++ files
    ".java", ".kt",                              # Java/Kotlin files
    ".rb",                                       # Ruby
    ".go",                                       # Go
    ".swift",                                    # Swift
    ".rs",                                       # Rust
    ".sh",                                       # Shell scripts
    ".json", ".xml", ".yml", ".yaml",             # Config-type files that are source-code
    ".pl",                                       # Perl
    ".php",                                      # PHP
    ".cs"                                        # C#
}

# Define a set of extensions that should be explicitly skipped (assets, images, etc.).
SKIP_EXTENSIONS = {
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bmp"
}

def get_git_tracked_files():
    """
    Returns a sorted list of all files tracked in the current repository and its submodules.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--recurse-submodules"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        files = result.stdout.splitlines()
        return sorted(files)
    except subprocess.CalledProcessError as e:
        print("Error while running git ls-files:", e.stderr, file=sys.stderr)
        sys.exit(1)

def is_source_file(filepath):
    """
    Returns True if the file is considered a source code file.
      - Its basename is not in EXCLUDE_FILES.
      - It doesn't start with a dot.
      - It isn't inside a directory named "migrations".
      - Its extension is in our whitelist of SOURCE_EXTENSIONS and not in SKIP_EXTENSIONS.
    """
    basename = os.path.basename(filepath)
    if basename in EXCLUDE_FILES:
        return False
    if basename.startswith("."):
        return False
    # Exclude if any directory in its path is named "migrations"
    parts = filepath.split(os.sep)
    if "migrations" in parts:
        return False
    extension = os.path.splitext(basename)[1].lower()
    # Exclude files with extensions that are in the SKIP_EXTENSIONS.
    if extension in SKIP_EXTENSIONS:
        return False
    # Accept the file only if its extension is in the SOURCE_EXTENSIONS whitelist.
    if extension in SOURCE_EXTENSIONS:
        return True

    # Optionally, if a file has no extension at all, you might want to include it if it is text.
    # For now, we exclude files without an extension.
    return False

def main():
    all_files = get_git_tracked_files()
    
    # Filter files by the source file check.
    included_files = [f for f in all_files if is_source_file(f)]
    
    # Print the list of files along with their count.
    print("The following source code files will be included in prompt.txt:")
    for filepath in included_files:
        print(filepath)
    print(f"\nTotal source code files: {len(included_files)}\n")
    
    # Write the output to prompt.txt with the required formatting.
    with open("prompt.txt", "w", encoding="utf-8") as outfile:
        for filepath in included_files:
            outfile.write(f"{filepath}:\n")
            outfile.write("```\n")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    outfile.write(f.read())
            except Exception as e:
                outfile.write(f"Error reading file: {e}")
            outfile.write("\n```\n")
            outfile.write("---------------------\n")
    
    print("prompt.txt has been created successfully.")

if __name__ == "__main__":
    main()