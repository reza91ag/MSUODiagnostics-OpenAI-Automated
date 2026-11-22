"""Serve the MSUO homepage on a dedicated localhost port."""
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    homepage = Path(__file__).with_name("index.html").read_bytes()

    class HomepageHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.serve_homepage(send_body=True)

        def do_HEAD(self):
            self.serve_homepage(send_body=False)

        def serve_homepage(self, *, send_body):
            if urlsplit(self.path).path != "/":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(homepage)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if send_body:
                self.wfile.write(homepage)

    with ThreadingHTTPServer(("127.0.0.1", args.port), HomepageHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
