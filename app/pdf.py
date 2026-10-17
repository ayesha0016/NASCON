import base64

def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

def make_pdf(lines):
    """Minimal valid PDF (no external library), uncompressed text."""
    y = 800
    parts = ["BT", "/F1 12 Tf"]
    for ln in lines:
        safe = ln.encode("latin-1", "replace").decode("latin-1")
        parts.append(f"1 0 0 1 50 {y} Tm ({_esc(safe)}) Tj")
        y -= 18
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return base64.b64encode(bytes(out)).decode()
