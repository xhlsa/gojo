"""
Setup file for compiling Cython accelerometer processor
Run: python setup.py build_ext --inplace
"""

from setuptools import setup
from Cython.Build import cythonize

setup(
    name='accel_processor',
    ext_modules=cythonize(
        "accel_processor.pyx",
        compiler_directives={
            'language_level': '3',
            'boundscheck': False,
            'wraparound': False,
            'cdivision': True,
        }
    ),
)
