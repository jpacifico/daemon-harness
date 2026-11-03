"""Cross-platform assertions for private test artifacts."""

import os

import pytest


@pytest.fixture
def assert_private_file():
    def check(path, *, fd=None, directory=False):
        if os.name != "nt":
            assert os.stat(path).st_mode & 0o777 == (0o700 if directory else 0o600)
            return
        import msvcrt
        import ntsecuritycon
        import win32api
        import win32con
        import win32security

        info = win32security.DACL_SECURITY_INFORMATION
        if fd is None:
            descriptor = win32security.GetFileSecurity(str(path), info)
        else:
            descriptor = win32security.GetSecurityInfo(
                msvcrt.get_osfhandle(fd),
                win32security.SE_FILE_OBJECT,
                info,
            )
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        assert (
            descriptor.GetSecurityDescriptorControl()[0]
            & win32security.SE_DACL_PROTECTED
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl is not None
        assert acl.GetAceCount() == 1
        flags = (
            (ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE)
            if directory
            else 0
        )
        assert acl.GetAce(0) == (
            (win32security.ACCESS_ALLOWED_ACE_TYPE, flags),
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )

    return check
