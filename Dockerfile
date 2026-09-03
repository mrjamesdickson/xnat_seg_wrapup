FROM python:3.12-slim

# Lightweight: no torch. Only what measuring masks and writing DICOM SEG needs.
RUN pip install --no-cache-dir "nibabel>=5" "numpy>=1.24" "pydicom>=3" "highdicom>=0.24" "pytest>=8"

COPY pyproject.toml README.md /opt/seg-wrapup/
COPY segwrapup /opt/seg-wrapup/segwrapup
RUN pip install --no-cache-dir --no-deps /opt/seg-wrapup

# The Container Service reads wrapup command definitions from this label when the
# image is pulled. Kept byte-identical to commands/seg-wrapup.json by tests/test_cli.py.
LABEL org.nrg.commands="[{\"name\":\"seg-wrapup\",\"description\":\"Post-process segmentation output: volumetrics report, ITK-SNAP/Slicer label files, DICOM SEG (registered with OHIF when the parent passes SEG_PROJECT/SEG_SESSION_ID) when the parent copied its source DICOM to /output/.source_dicom\",\"version\":\"0.2.3\",\"type\":\"docker-wrapup\",\"image\":\"xnatworks/seg-wrapup:0.2.3\",\"command-line\":\"seg-wrapup\",\"mounts\":[],\"inputs\":[],\"outputs\":[],\"xnat\":[]}]"

RUN mkdir -p /input /output
# No ENTRYPOINT: the Container Service runs a wrapup with its own command-line
# (here "seg-wrapup") and cannot override an entrypoint, so an entrypoint would
# make the container run "seg-wrapup seg-wrapup" and fail on argument parsing.
CMD ["seg-wrapup"]
