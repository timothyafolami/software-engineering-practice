"""
Layer 7 · Topic 3 — XSS by output context (Python / Jinja2).

One command, no arguments: `python3 xss_context.py`.
Jinja2's autoescape escapes for ONE context -- HTML body -- and does not
parse the surrounding HTML, so it cannot know where a value landed. This
program renders the SAME attacker payload into four contexts, twice (with
autoescape ON, the FastAPI/`.html` default, and OFF, the bare Environment()
trap), and prints the exact emitted bytes plus whether the payload would
execute in a browser.

The execute verdict is a STATIC determination from the emitted bytes (the
full lab fires these in a real headless browser and counts beacon bytes):
body/attribute/href are adjudicated by parsing the output with stdlib
html.parser -- so an entity-escaped &#34; correctly reads as text, not an
attribute boundary -- and the <script> break-out is judged by whether a
literal </script> was injected.

What to look for: with autoescape ON, body and the two break-outs are
neutralized, but the `javascript:` URL still EXECUTES -- Jinja escaped the
HTML metacharacters (there are none in `javascript:beacon()`) and has no idea
`href` is a URL context. That single surviving cell is the whole lesson, and
it is exactly the cell Go's html/template turns into #ZgotmplZ.
"""
from html.parser import HTMLParser
from jinja2 import Environment


class TagScan(HTMLParser):
    """Collects (tag, attrs-dict) and script data from emitted HTML.
    Attribute values arrive already entity-decoded, and <script> content is
    read in CDATA mode -- both match how a browser tokenizes."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.script_data = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.set_cdata_mode("script")

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_data.append(data)


def executes(context, emitted):
    scan = TagScan()
    scan.feed(emitted)
    if context == "body":
        # A live <script> element in the body executes.
        return "<script>beacon()</script>" in emitted
    if context == "attr":
        # A real event-handler attribute (not text inside a value).
        return any(any(a.startswith("on") and "beacon" in v for a, v in attrs.items())
                   for _tag, attrs in scan.tags)
    if context == "href":
        # An href whose scheme is javascript: after decoding.
        return any(v.strip().lower().startswith("javascript:") and "beacon" in v
                   for _tag, attrs in scan.tags for a, v in attrs.items() if a == "href")
    if context == "script":
        # The injection closed the script and opened a new one.
        return emitted.lower().count("</script>") > 1
    return False


CELLS = [
    ("body",   "body-context <script>", "<div>{{ x }}</div>",              "<script>beacon()</script>"),
    ("attr",   "attribute handler",     "<a title={{ x }}>x</a>",          "y onmouseover=beacon()"),
    ("href",   "javascript: URL",       '<a href="{{ x }}">x</a>',         "javascript:beacon()"),
    ("script", "</script> break-out",   '<script>var c="{{ x }}";</script>', "</script><script>beacon()</script>"),
]


def run(autoescape):
    env = Environment(autoescape=autoescape)
    label = "autoescape ON " if autoescape else "autoescape OFF"
    print(f"  --- {label} (Jinja2) ---")
    print(f"    {'context':<24}{'executes?':<10}emitted bytes")
    executed = 0
    for ctx, name, tmpl, payload in CELLS:
        out = env.from_string(tmpl).render(x=payload)
        ex = executes(ctx, out)
        executed += ex
        print(f"    {name:<24}{('YES' if ex else 'NO'):<10}{out}")
    print(f"    -> {executed}/4 payloads executed\n")


def main():
    print("Layer 7 · Topic 3 — XSS by output context (Python / Jinja2)\n")
    run(autoescape=True)
    run(autoescape=False)
    print("Read: autoescape ON stops the body and both break-outs, because it "
          "escapes <, >, \" and '. It does NOT stop the javascript: URL -- that "
          "payload has no HTML metacharacters, so HTML escaping is a no-op, and "
          "Jinja never modeled that href is a URL context. autoescape OFF (the "
          "bare Environment() default) executes all four. Compare Go's "
          "html/template, which neutralizes the URL cell too.")


if __name__ == "__main__":
    main()
