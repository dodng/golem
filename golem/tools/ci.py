import os


def in_appveyor():
    return os.environ.get('APPVEYOR', False)


def in_travis():
    return os.environ.get('TRAVIS', False)


def ci_skip(obj):
    import unittest
    if in_appveyor() or in_travis():
        return unittest.skip('Unsupported CI environment')
    return obj
