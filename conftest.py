"""
conftest.py  — project root
Adds the project root to sys.path so that `contrast_worker` and `api_server`
are importable regardless of where pytest is invoked from.
"""
import sys
import os

# Insert project root at the front of sys.path
sys.path.insert(0, os.path.dirname(__file__))