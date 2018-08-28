import setuptools

with open("README.md", "r") as f:
    long_description = f.read()

setuptools.setup(
    name="virtool.pathoscope",
    version="0.1.0",
    author="Ian Boyes",
    author_email="ian.boyes@canada.ca",
    description="A package for running Virtool Pathoscope analysis in several environments",
    license="MIT",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/virtool/virtool.pathoscope",
    packages=[
        "virtool.pathoscope"
    ],
    python_requires=">=3.5",
    classifiers=(
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "License :: OSI Approved :: MIT License",
        "Topic :: Scientific/Engineering :: Bio-Informatics"
    )
)
