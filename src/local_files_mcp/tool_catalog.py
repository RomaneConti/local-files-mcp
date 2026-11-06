"""One expected read-only tool surface for server smoke/health checks."""
EXPECTED_TOOLS = {'list_roots', 'list_directory', 'find_files', 'search_text',
    'file_info', 'read_text', 'read_json', 'inspect_array', 'read_array',
    'inspect_file', 'read_table', 'read_document', 'search_file', 'read_image', 'read_archive_member'}
