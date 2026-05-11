import argparse
import os
import subprocess
import sys


def install_packages_from_requirements(requirements_file):
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", requirements_file, "--upgrade"], check=True)
    print("Packages installed successfully.")


def load_env_variables(env_filename="subgen.env"):
    try:
        with open(env_filename, "r") as file:
            for line in file:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    var, value = line.split("=", 1)
                    os.environ[var] = value
        print(f"Environment variables have been loaded from {env_filename}")
    except FileNotFoundError:
        print(f"{env_filename} file not found. Continuing with process environment.")


def main():
    if sys.version_info < (3, 10):
        print(f"This script requires Python 3.10 or higher, you are running {sys.version}")
        sys.exit(1)

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser(prog="python launcher.py", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-i", "--install", action="store_true", help="Install/update Python packages")
    parser.add_argument("-a", "--append", action="store_true", help="Append a Subgen watermark to generated SRT subtitles")
    parser.add_argument("-x", "--exit-early", action="store_true", help="Exit without running subgen.py")
    args = parser.parse_args()

    load_env_variables()

    if args.debug:
        os.environ["DEBUG"] = "True"
    elif "DEBUG" not in os.environ:
        os.environ["DEBUG"] = "False"

    if args.append:
        os.environ["APPEND"] = "True"
    elif "APPEND" not in os.environ:
        os.environ["APPEND"] = "False"

    if args.install:
        install_packages_from_requirements("requirements.txt")

    if args.exit_early:
        print("Not running subgen.py: -x or --exit-early set")
        return

    print("Launching subgen.py")
    subprocess.run([sys.executable, "-u", "subgen.py"], check=True)


if __name__ == "__main__":
    main()
