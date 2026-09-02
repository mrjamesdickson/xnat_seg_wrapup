import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROWS, COLS, SLICES = 12, 16, 6
PIXEL_SPACING = (0.8, 0.7)  # row, column
SLICE_GAP = 2.5


def write_mask(path, array, affine=None, voxel_size=(1.0, 1.0, 1.0)):
    if affine is None:
        affine = np.diag([*voxel_size, 1.0])
    nib.save(nib.Nifti1Image(array.astype(np.uint8), affine), str(path))
    return path


def write_ct_series(directory: Path, slices: int = SLICES) -> list[Dataset]:
    """A minimal but valid CT series: identity orientation, origin at (10, 20, 30) LPS."""
    directory.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = generate_uid(), generate_uid(), generate_uid()
    datasets = []
    for index in range(slices):
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.PatientName = "Test^Wrapup"
        ds.PatientID = "WRAPUP001"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyInstanceUID = study_uid
        ds.StudyID = "1"
        ds.StudyDate = "20260101"
        ds.StudyTime = "120000"
        ds.AccessionNumber = ""
        ds.ReferringPhysicianName = ""
        ds.SeriesInstanceUID = series_uid
        ds.SeriesNumber = 2
        ds.Modality = "CT"
        ds.FrameOfReferenceUID = frame_uid
        ds.PositionReferenceIndicator = ""
        ds.InstanceNumber = index + 1
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [10.0, 20.0, 30.0 + index * SLICE_GAP]
        ds.PixelSpacing = list(PIXEL_SPACING)
        ds.SliceThickness = SLICE_GAP
        ds.Rows, ds.Columns = ROWS, COLS
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.RescaleIntercept = -1024
        ds.RescaleSlope = 1
        ds.PixelData = np.zeros((ROWS, COLS), dtype=np.int16).tobytes()
        # Write out of order on disk to prove sorting is by position, not filename.
        ds.save_as(str(directory / f"slice_{slices - index:03d}.dcm"), enforce_file_format=True)
        datasets.append(ds)
    return datasets


def series_ras_affine():
    """Voxel (col,row,slice) -> RAS for write_ct_series, the geometry dcm2niix would emit."""
    affine = np.eye(4)
    affine[0, 0] = -PIXEL_SPACING[1]
    affine[1, 1] = -PIXEL_SPACING[0]
    affine[2, 2] = SLICE_GAP
    affine[:3, 3] = [-10.0, -20.0, 30.0]
    return affine


def blob_mask():
    """(col,row,slice) array: label 3 in cols 5-9, rows 3-6, slices 2-4; label 7 in one corner voxel."""
    data = np.zeros((COLS, ROWS, SLICES), dtype=np.uint8)
    data[5:10, 3:7, 2:5] = 3
    data[0, 0, 0] = 7
    return data


@pytest.fixture
def ct_series(tmp_path):
    directory = tmp_path / "dicom"
    datasets = write_ct_series(directory)
    return directory, datasets


@pytest.fixture
def read_dicom():
    return lambda path: pydicom.dcmread(str(path))
