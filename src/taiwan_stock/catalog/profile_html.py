"""Small HTML tree for labelled official product fields (no browser dependency)."""

import re
from html.parser import HTMLParser


class Node:
    def __init__(self, tag="root", attrs=(), parent=None, index=0):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.index = index
        self.children = []

    def text(self):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return " ".join(
            " ".join(
                c.text() if isinstance(c, Node) else c for c in self.children
            ).split()
        )

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def next_element(self):
        if self.parent:
            siblings = self.parent.children
            start = (
                self.index + 1
                if 0 <= self.index < len(siblings) and siblings[self.index] is self
                else siblings.index(self) + 1
            )
            for sibling in siblings[start:]:
                if isinstance(sibling, Node):
                    return sibling
        return None


class Tree(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current, len(self.current.children))
        self.current.children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(
            Node(tag, attrs, self.current, len(self.current.children))
        )

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def compact(text):
    return re.sub(r"[\s：:]", "", text)


def fields(root, *labels, tags=None):
    """Exact label and its sibling value; never crosses into another table row."""
    wanted = {compact(label) for label in labels}
    result = []
    for node in root.nodes():
        if tags and node.tag not in tags:
            continue
        if compact(node.text()) not in wanted:
            continue
        sibling = node.next_element()
        if sibling and (value := sibling.text()) and value not in result:
            result.append(value)
    return result


def field(root, *labels, tags=None):
    values = fields(root, *labels, tags=tags)
    if len(values) > 1:
        raise ValueError(f"官方欄位 {labels[0]} 有多個不同值，待核對")
    return values[0] if values else None
