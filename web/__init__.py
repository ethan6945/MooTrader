# Package marker. Only reason it exists: the frozen build imports the server as
# `web.server` (packaging/entry.py) instead of running web/server.py as a script,
# and an implicit namespace package doesn't survive PyInstaller's analysis.
