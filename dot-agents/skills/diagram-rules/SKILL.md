---
name: diagram-rules
description: Use when drawing an architecture, flow or dependency diagram (SVG, Mermaid, Artifact or .excalidraw); adds evidence rules and an editable Excalidraw output mode.
---

# Diagram rules

Add these to whatever diagram tool you use, including the built-in Artifact diagram skills.

## Explain first, then structure
Write a short architecture explanation (a few sentences) before drawing. Build the nodes and edges from that text, not straight from the files.

## Every edge cites evidence
- Each edge names the file or line that supports it.
- If the code you read does not show a relationship, draw the edge dashed and label it "inferred".
- The caller owns the edge. Do not chain siblings into a pipeline. Labels are 2 to 4 words. Use 3 to 6 groups.

## Editable output: `.excalidraw`
Choose by who will use it: editable or shared for edits goes to a `.excalidraw` file; a polished standalone picture goes to SVG.
- Put each bound text element straight after its container.
- Shapes at least 120 by 60, text 16 px, `roughness: 1`.
