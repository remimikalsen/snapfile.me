#!/usr/bin/env python3
import sys

import xml.etree.ElementTree as ET

def get_coverage_percentage(xml_path):
    """Parses the coverage XML and returns the integer percentage."""
    try:
        tree = ET.parse(xml_path)
    except Exception as e:
        sys.exit(f"Error parsing XML file: {e}")
    root = tree.getroot()
    try:
        line_rate = float(root.attrib.get("line-rate", 0))
    except Exception as e:
        sys.exit(f"Error reading line-rate from XML: {e}")
    return int(round(line_rate * 100))

def generate_badge_svg(coverage):
    """Generates an SVG badge string with the given coverage percentage."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="20">
  <linearGradient id="a" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <rect rx="3" width="120" height="20" fill="#555"/>
  <rect rx="3" x="70" width="50" height="20" fill="#4c1"/>
  <path fill="#4c1" d="M70 0h4v20h-4z"/>
  <rect rx="3" width="120" height="20" fill="url(#a)"/>
  <g fill="#fff" text-anchor="middle" font-family="Verdana" font-size="11">
    <text x="35" y="14">coverage</text>
    <text x="95" y="14">{coverage}%</text>
  </g>
</svg>'''

def main():
    # Default paths
    coverage_xml = "coverage.xml"
    badge_file = "coverage-badge.svg"

    # Process arguments: either no argument or a single "print"
    args = sys.argv[1:]
    if len(args) > 1:
        sys.exit(f"Usage: {sys.argv[0]} [print]")

    print_badge = (len(args) == 1 and args[0] == "print")
    if len(args) == 1 and args[0] != "print":
        sys.exit(f"Usage: {sys.argv[0]} [print]")

    coverage = get_coverage_percentage(coverage_xml)
    svg = generate_badge_svg(coverage)

    if print_badge:
        print(svg, end='')
    else:
        # Write the SVG badge to file. If the file exists and content is identical, it will be overwritten.
        with open(badge_file, 'w') as f:
            f.write(svg)

if __name__ == "__main__":
    main()
