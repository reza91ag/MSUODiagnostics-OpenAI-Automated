"""Serve the public MSUO website on a dedicated localhost port."""
from argparse import ArgumentParser
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from struct import pack
from urllib.parse import urlsplit


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    directory = Path(__file__).parent
    html = "text/html; charset=utf-8"
    methodology = (html, (directory / "methodology.html").read_bytes())
    routes = {
        "/": (html, (directory / "index.html").read_bytes()),
        "/methodology": methodology,
        "/methodology/": methodology,
    }
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url><loc>https://www.msuo-runtime.io/</loc></url>\n'
        '  <url><loc>https://www.msuo-runtime.io/methodology/</loc></url>\n'
        '</urlset>\n'
    ).encode("utf-8")
    robots = (
        'User-agent: *\n'
        'Allow: /\n'
        'Sitemap: https://www.msuo-runtime.io/sitemap.xml\n'
    ).encode("utf-8")
    favicon = b64decode((directory / "favicon.png.b64").read_text(encoding="ascii").strip())
    routes["/favicon.png"] = ("image/png", favicon)
    # ICO container with the original 96px PNG; supports legacy icon requests.
    ico = pack("<HHH", 0, 1, 1) + pack("<BBBBHHII", 96, 96, 0, 0, 1, 32, len(favicon), 22) + favicon
    routes["/favicon.ico"] = ("image/x-icon", ico)
    routes["/sitemap.xml"] = ("application/xml; charset=utf-8", sitemap)
    routes["/robots.txt"] = ("text/plain; charset=utf-8", robots)
    for number in range(1, 4):
        name = f"figure-{number}.webp"
        routes[f"/methodology/{name}"] = ("image/webp", (directory / name).read_bytes())

    class HomepageHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.serve_page(send_body=True)

        def do_HEAD(self):
            self.serve_page(send_body=False)

        def serve_page(self, *, send_body):
            page = routes.get(urlsplit(self.path).path)
            if page is None:
                self.send_error(404)
                return
            content_type, content = page
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if send_body:
                self.wfile.write(content)

    with ThreadingHTTPServer(("127.0.0.1", args.port), HomepageHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
