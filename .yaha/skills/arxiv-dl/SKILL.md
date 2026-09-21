---
name: arxiv-dl
description: Use this skill when you want to download a paper (pdf version) from arXiv site.
resources:
---

# Instructions

- **Locate the Document:** Access the arXiv abstract page for the target paper and extract the direct URL link to the PDF version.
    
- **Generate the Target Filename:** Select two descriptive keywords from the paper’s title and construct the output filename using the following format: `<arxiv-id>-<keyword1>-<keyword2>.pdf`
    
- **Download the PDF:** Execute a `curl -L` command to download the PDF directly into your target directory using the newly generated filename.
