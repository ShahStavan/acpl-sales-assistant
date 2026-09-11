"""Read the raw data pack: 7 CSV exports, ``action_playbook.xlsx`` and 6 ``.docx`` documents.

Dates are parsed with explicit formats per source (ISO, ``YYYY-MM``, ``DD/MM/YYYY``).
Documents are held whole and tagged with brands, regions, distributors and months by
vocabulary match. DESIGN.md §4.4 item 4, §4.5.
"""
