"""Package setup."""

from pathlib import Path
from setuptools import setup, find_packages

root_dir = Path(__file__).parent.resolve()

exec(open(root_dir / "foca" / "version.py").read())

file_name = root_dir / "README.md"
with open(file_name, "r") as _file:
    long_description = _file.read()

install_requires = []
req = root_dir / 'requirements.txt'
with open(req, "r") as _file:
    install_requires = _file.read().splitlines()

setup(
    name="foca",
    version=__version__,  # noqa: F821
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
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
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
    package_data={"foca.security.access_control.api": ["*.yaml", "*.conf"]},
    setup_requires=[
        "setuptools_git==1.2",
        "twine==3.8.0"
    ],
)
