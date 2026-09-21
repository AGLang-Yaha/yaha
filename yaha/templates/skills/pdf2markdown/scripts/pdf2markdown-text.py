# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import argparse
import sys
from pathlib import Path
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

def convert_pdf_to_md(pdf_path: str, output_path: str = None):
    """
    Converts a PDF file to Markdown using Docling, keeping only text content.
    OCR is disabled for performance and to avoid dependency issues.
    """
    try:
        # Initialize pipeline options and disable OCR
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        
        # Initialize the converter with the custom options for PDF
        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        
        # Convert the document
        print(f"Converting {pdf_path}...", file=sys.stderr)
        result = converter.convert(pdf_path)
        
        # Export to Markdown
        # docling's export_to_markdown focuses on structural text and tables, 
        # and excludes images by default.
        markdown_content = result.document.export_to_markdown()
        
        # Save or print the output
        if output_path:
            # Ensure parent directory exists
            output_file = Path(output_path)
            output_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            print(f"Successfully converted to {output_path}", file=sys.stderr)
        else:
            print(markdown_content)
            
    except Exception as e:
        print(f"Error during conversion: {e}", file=sys.stderr)
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Convert PDF to Markdown using Docling (text only, no OCR).")
    parser.add_argument("input", help="Path to the input PDF file.")
    parser.add_argument("-o", "--output", help="Path to the output Markdown file (optional).")
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File {args.input} does not exist.", file=sys.stderr)
        sys.exit(1)
        
    convert_pdf_to_md(str(input_path), args.output)

if __name__ == "__main__":
    main()
