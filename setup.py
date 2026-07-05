#!/usr/bin/env python3
"""Build/install script for brdf2csv with the optional Cython fast path.

Works in three modes:
  1. Cython installed  -> compiles _brdfc.pyx (regenerates C)
  2. No Cython         -> compiles the bundled pre-generated _brdfc.c
  3. No C compiler     -> pip install still succeeds; the extension is
                          simply skipped and the pure-Python module is used

Usage:
  python3 setup.py build_ext --inplace   # build _brdfc.so next to the sources
  pip install .                          # install brdf2csv + extension
"""

import os
import sys

from setuptools import setup
from setuptools.extension import Extension
from setuptools.command.build_ext import build_ext


HERE = os.path.abspath(os.path.dirname(__file__))

# Prefer .pyx when Cython is available; otherwise use the bundled .c
try:
    from Cython.Build import cythonize

    ext_modules = cythonize(
        [Extension("_brdfc", [os.path.join(HERE, "_brdfc.pyx")])],
        language_level=3,
    )
except ImportError:
    c_file = os.path.join(HERE, "_brdfc.c")
    if os.path.exists(c_file):
        ext_modules = [Extension("_brdfc", [c_file])]
    else:
        sys.stderr.write(
            "warning: neither Cython nor _brdfc.c available; "
            "building without the fast path\n"
        )
        ext_modules = []


class OptionalBuildExt(build_ext):
    """Make the extension optional: fall back to pure Python on any
    compiler failure instead of aborting the install."""

    def run(self):
        try:
            build_ext.run(self)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                "warning: could not build the _brdfc extension (%s); "
                "brdf2csv will use the pure-Python parser\n" % exc
            )

    def build_extension(self, ext):
        try:
            build_ext.build_extension(self, ext)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                "warning: could not build %s (%s); continuing without it\n"
                % (ext.name, exc)
            )


setup(
    name="brdf2csv",
    version="1.0.0",
    description="Convert RDF4J Binary RDF (GraphDB exports) to CSV",
    py_modules=["brdf2csv"],
    ext_modules=ext_modules,
    cmdclass={"build_ext": OptionalBuildExt},
    python_requires=">=3.6",
    entry_points={"console_scripts": ["brdf2csv=brdf2csv:main"]},
)
