"""File reader modules for the book-companion agent."""
from file_readers.epub_reader import EpubReader, MobiReader, open_book
from file_readers.html_skeleton import extract_html_skeleton, split_html_by_heading

__all__ = [
    "EpubReader", "MobiReader", "open_book",
    "extract_html_skeleton", "split_html_by_heading",
]
