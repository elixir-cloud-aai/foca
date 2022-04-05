import os
from setuptools import setup, find_packages

from foca import __version__

root_dir = os.path.dirname(os.path.abspath(__file__))

# Read long description from file
file_name = os.path.join(root_dir, "README.md")
with open(file_name, "r") as fh:
    long_description = fh.read()

# Read requirements from file
install_requires = []
req = root_dir + '/requirements.txt'
if os.path.isfile(req):
    with open(req) as f:
        install_requires = f.read().splitlines()

setup(
    name="foca",
    version=__version__,
    description=(
        "Archetype for OpenAPI microservices based on Flask and Connexion"
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/elixir-cloud-aai/foca",
    author="ELIXIR Cloud & AAI",
    author_email="alexander.kanitz@alumni.ethz.ch",
    maintainer="Alexander Kanitz",
    maintainer_email="alexander.kanitz@alumnni.ethz.ch",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Environment :: Web Environment",
        "Framework :: Flask",
        "Intended Audience :: Developers",
        "Intended Audience :: Information Technology",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: System :: Systems Administration",
        "Topic :: Utilities",
        "Typing :: Typed",
    ],
    keywords=(
        'rest api app openapi python microservice'
    ),
    project_urls={
        "Repository": "https://github.com/elixir-cloud-aai/foca",
        "ELIXIR Cloud & AAI": "https://elixir-europe.github.io/cloud/",
        "Tracker": "https://github.com/elixir-cloud-aai/foca/issues",
    },
    packages=find_packages(),
    install_requires=install_requires,
    include_package_data=True,
    setup_requires=[
        "setuptools_git==1.2",
        "twine==3.8.0"
    ],
)
