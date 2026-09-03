"""Shared post-processing for XNAT segmentation containers.

Runs as a Container Service wrapup command: reads whatever masks a model wrote,
and produces the volumetrics report, viewer label files, and (when the source
DICOM is available) a DICOM SEG, in one consistent resource layout.
"""

__version__ = "0.2.3"
