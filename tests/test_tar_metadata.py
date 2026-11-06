import io
import tarfile
import pytest
from local_files_mcp.readers import archive


def test_pax_recursion_is_bounded_before_tarfile_recurses():
    info = tarfile.TarInfo('pax')
    info.type = tarfile.XHDTYPE
    info.size = 0
    payload = info.tobuf()*1500 + b'\0'*1024
    with pytest.raises(ValueError, match='extension headers'):
        archive.inspect(io.BytesIO(payload), '.tar')


def test_safe_pax_name_and_sparse_rejection():
    for extra in ({'mtime':'1.5'}, {'GNU.sparse.map':'0,1'}):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as tar:
            info=tarfile.TarInfo('a.txt')
            info.pax_headers=extra
            info.size=5
            tar.addfile(info,io.BytesIO(b'hello'))
        stream.seek(0)
        if 'mtime' in extra:
            assert archive.read(stream,'.tar','a.txt')['text']=='hello'
        else:
            with pytest.raises(ValueError,match='Sparse'):
                archive.inspect(stream,'.tar')
