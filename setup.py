#!/usr/bin/env python3
"""
tfsm_fire - TextFSM Auto-Detection Engine
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text(encoding="utf-8")

setup(
    name="tfsm-fire",
    version="0.1.0",
    author="Scott Peterman",
    author_email="scottpeterman@gmail.com",
    description="Automatically find and apply the best TextFSM template for your network device output",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/scottpeterman/tfsm_fire",
    project_urls={
        "Bug Tracker": "https://github.com/scottpeterman/tfsm_fire/issues",
        "Source": "https://github.com/scottpeterman/tfsm_fire",
    },
    license="GPL-3.0-or-later",
    packages=find_packages(),
    include_package_data=True,
    python_requires=">=3.10",
    install_requires=[
        "textfsm>=1.1.3",
        "click>=8.0",
    ],
    extras_require={
        "gui": [
            "PyQt6>=6.4",
            "requests>=2.28",
        ],
    },
    entry_points={
        "console_scripts": [
            "tfsm-gui=tfire.tfsm_gui:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "Environment :: X11 Applications :: Qt",
        "Intended Audience :: System Administrators",
        "Intended Audience :: Telecommunications Industry",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Networking",
        "Topic :: System :: Systems Administration",
        "Topic :: Text Processing :: Filters",
    ],
    keywords="textfsm network automation parsing cisco arista juniper",
)