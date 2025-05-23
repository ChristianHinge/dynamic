#!/usr/bin/python
"""
Script for extracting volumes of interest (VOIs) from different aorta segments
using a pre-segmented aorta mask
"""

import numpy as np
import nibabel as nib
from scipy.ndimage import (
    center_of_mass, 
    label, 
    uniform_filter, 
    binary_dilation, 
    median_filter
)
from skimage.measure import regionprops
from pathlib import Path
from nifti_dynamic.utils import img_to_array_or_dataobj
from enum import Enum

class AortaSegment(Enum):
    ASCENDING = 1
    TOP = 2
    DESCENDING = 3
    DESCENDING_BOTTOM = 4


def count_connected_components(slice_2d):
    """Count connected components in a 2D slice"""
    labeled, num = label(slice_2d)
    return num


def count_axial_components(volume):
    """Count connected components along z-axis for each slice"""
    return np.array([count_connected_components(volume[..., sl]) for sl in range(volume.shape[-1])])


def find_pattern_transition(array, pattern):
    """
    Find index where pattern matches in array
    Returns the middle point of the transition
    """
    indices = np.where(np.all(np.lib.stride_tricks.sliding_window_view(array, len(pattern)) == pattern, axis=1))[0]
    if len(indices) != 1:
        raise ValueError(f"Expected 1 match, found {len(indices)}")
    
    return indices[0] + len(pattern) // 2


def find_aortic_segments_boundaries(aorta_volume):
    """
    Identify axial indices where aorta transitions between segments
    Returns the start and curve indices
    """
    # Calculate transitions between different aortic segments
    islands = count_axial_components(aorta_volume)
    islands = np.minimum(islands, 2)  # Cap max islands at 2
    islands = median_filter(islands, size=5)  # Smooth counts
    
    # Find transition points 1 -> 2 islands, 2->1 islands
    ix_start = find_pattern_transition(islands, np.array([1, 1, 2, 2]))
    ix_curve = find_pattern_transition(islands, np.array([2, 2, 1, 1]))

    return ix_start, ix_curve


def segment_aorta(aorta):
    # Segment aorta into four anatomical regions
    aorta = aorta.copy()
    ix_start, ix_curve = find_aortic_segments_boundaries(aorta)
    
    # Process pre-curve region
    aorta_seg = aorta.copy().astype(np.int64)
    aorta_seg[..., ix_curve:] = 0
    
    # Label components and determine ascending/descending parts
    labeled, _ = label(aorta_seg)
    
    # Determine which label is ascending vs descending based on volume
    if np.sum(labeled == 1) > np.sum(labeled == 2):
        mapping = np.array([0, AortaSegment.DESCENDING.value, AortaSegment.ASCENDING.value])
    else:
        mapping = np.array([0, AortaSegment.ASCENDING.value, AortaSegment.DESCENDING.value])
    
    aorta_seg = mapping[labeled]
    
    # Mark top section
    aorta_seg[..., ix_curve:] = aorta[..., ix_curve:] * AortaSegment.TOP.value
    
    # Mark descending bottom section
    mask = (aorta_seg == AortaSegment.DESCENDING.value) & (np.arange(aorta.shape[-1]) < ix_start)[None, None, :]
    aorta_seg[mask] = AortaSegment.DESCENDING_BOTTOM.value

    return aorta_seg.astype(np.uint8)


def create_cylindrical_voi(aorta_segment, pet, voxel_size, volume_ml=1.0, cylinder_width=3):
    """
    Create a cylindrical VOI inside the specified aorta segment
    
    Parameters:
    -----------
    aorta_segment : numpy.ndarray
        Binary mask of the aorta segment
    pet : numpy.ndarray
        PET image data
    voxel_size : tuple or array-like
        Voxel dimensions in mm
    volume_ml : float
        Target volume in milliliters (default: 1.0)
    cylinder_width : int
        Width of the cylindrical cross-section (default: 3)
        
    Returns:
    --------
    numpy.ndarray
        Binary mask of the VOI
    """
    # Calculate target volume in voxels
    voxel_volume = np.prod(voxel_size)
    target_voxels = int(volume_ml * 1000 / voxel_volume)
    
    # Create empty VOI
    voi = np.zeros_like(aorta_segment, dtype=bool)
    
    # Calculate required slices for target volume
    voxels_per_slice = cylinder_width**2
    n_slices_needed = max(1, int(np.ceil(target_voxels / voxels_per_slice)))

    # Find optimal placement based on PET uptake
    pet_masked = np.ma.masked_array(data=pet, mask=~aorta_segment)
    median_axial_uptake = np.ma.median(pet_masked, axis=(0,1)).filled(0)
    median_axial_uptake = uniform_filter(median_axial_uptake, n_slices_needed)
    start_slice = np.argmax(median_axial_uptake) - n_slices_needed//2

    # Place VOI seeds at center of mass for each slice
    pet_masked = pet_masked.filled(0)
    for slc in range(start_slice, start_slice+n_slices_needed):
        x, y = center_of_mass(pet_masked[..., slc])
        x, y = int(round(x)), int(round(y))
        voi[x, y, slc] = True

    # Create cylindrical shape by dilating the seed points
    dilation_mask = np.ones((cylinder_width, cylinder_width, 1), dtype=bool)
    voi = binary_dilation(voi, dilation_mask)
    
    # Calculate actual volume achieved
    actual_volume_ml = np.sum(voi) * voxel_volume / 1000
    print(f"Created cylinder of length {n_slices_needed} with volume: {actual_volume_ml:.2f} ml (target: {volume_ml:.2f} ml)")
    
    return voi


def average_early_pet_frames(dpet, frame_times_start, t_threshold=40):
    """
    Average PET frames up to the specified time threshold
    
    Parameters:
    -----------
    dpet : nibabel.Nifti1Image
        Dynamic PET image
    frame_times_start : numpy.ndarray
        Start times for each frame
    t_threshold : int
        Time threshold in seconds (default: 40)
        
    Returns:
    --------
    numpy.ndarray
        Averaged early PET frames
    """
    pet_arr = img_to_array_or_dataobj(dpet)
    n_frames = np.sum(frame_times_start < t_threshold)
    return pet_arr[..., :n_frames].mean(axis=-1)


def refine_aorta_with_pet_uptake(aorta, pet):
    """
    Refine aorta segmentation based on PET uptake
    Only keeps voxels with sufficient activity compared to median aorta uptake
    """
    pet_median_aorta = np.median(pet[aorta > 0])
    activity_mask = pet > (2/3 * pet_median_aorta)
    aorta_refined = aorta.copy()
    aorta_refined[~activity_mask] = 0
    return aorta_refined


def extract_aorta_vois(aorta_mask, affine, dpet, frame_times_start, t_threshold=40, volume_ml=1.0, cylinder_width=3):
    """
    Extract VOIs from different aorta segments
    
    Parameters:
    -----------
    aorta_mask : numpy.ndarray
        Binary mask of the aorta from totalsegmentator
    affine : numpy.ndarray
        Affine matrix of the aorta image
    dpet : nibabel.Nifti1Image
        Dynamic PET image
    frame_times_start : numpy.ndarray
        Start times for each frame
    t_threshold : int
        Time threshold for early frames in seconds (default: 40)
    volume_ml : float
        Target volume in milliliters (default: 1.0)
    cylinder_width : int
        Width of the cylindrical cross-section (default: 3)
        
    Returns:
    --------
    tuple
        (VOIs mask, segmented aorta mask)
    """
    # Average early PET frames and refine aorta segmentation
    pet_40s = average_early_pet_frames(dpet, frame_times_start, t_threshold)
    aorta_segments = segment_aorta(aorta_mask)
    aorta_segments = refine_aorta_with_pet_uptake(aorta_segments, pet_40s)
    
    # Calculate voxel size from affine matrix
    voxel_size = np.abs(np.diag(affine[:3, :3]))
    
    # Initialize VOIs mask
    vois = np.zeros_like(aorta_segments, dtype=np.int16)

    # Create VOIs for each aorta segment
    for seg in AortaSegment:
        aorta_segment = aorta_segments == seg.value
        print("Extracting VOI for", seg.name)
        if seg == AortaSegment.TOP:
            voi = create_cylindrical_voi(aorta_segment.swapaxes(1,2), pet_40s.swapaxes(1,2), voxel_size=voxel_size[[0,2,1]], 
                                        volume_ml=volume_ml, cylinder_width=cylinder_width)
            voi = voi.swapaxes(1,2)
        else:
            voi = create_cylindrical_voi(aorta_segment, pet_40s, voxel_size=voxel_size, 
                                        volume_ml=volume_ml, cylinder_width=cylinder_width)
        vois[voi] = seg.value

    return vois, aorta_segments