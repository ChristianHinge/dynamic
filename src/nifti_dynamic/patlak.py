from scipy.ndimage import gaussian_filter
import numpy as np
from tqdm import tqdm
from scipy.integrate import cumulative_simpson
from sklearn.linear_model import LinearRegression
from .utils import OverlappedChunkIterator, img_to_array_or_dataobj
def _voxel_patlak_chunk(arr,input_fun,t,n_frames_linear_regression=10):
   
    # Normalized cumsum AIF
    with np.errstate(divide='ignore',invalid='ignore'):
        _X = cumulative_simpson(input_fun,x=t/60,initial=0) / input_fun
        X = _X[-n_frames_linear_regression:].reshape(-1,1)

    # Normalized voxel response
    Y = arr.reshape(-1, arr.shape[-1]).T[-n_frames_linear_regression:]
    Y = Y/input_fun[-n_frames_linear_regression:,None]

    #Linear regression
    reg = LinearRegression().fit(X, Y)
    slopes = reg.coef_.reshape(arr.shape[:-1])
    intercepts = reg.intercept_.reshape(arr.shape[:-1])

    return slopes, intercepts

def voxel_patlak(img, input_fun, t, gaussian_filter_size=0, n_frames_linear_regression=10, axial_chunk_size=8):
    """
    Process image data in overlapping chunks, applying Gaussian smoothing and keeping only valid center portions.
    
    Args:
        img: Input 4D image array
        input_fun: Input function for Patlak analysis
        t: Time points
        gaussian_std: Standard deviation for Gaussian smoothing (default: 0)
        n_frames_linear_regression: Number of frames for linear regression (default: 10)
        axial_chunk_size: Size of axial chunks to process (default: 8)
    """
    img = img_to_array_or_dataobj(img)
    out = np.zeros(img.shape[:-1])
    border_size = 3 * gaussian_filter_size if gaussian_filter_size > 0 else 0
    
    # Create iterator for overlapped chunks
    chunk_iterator = OverlappedChunkIterator(
        array_size=img.shape[-2],
        chunk_size=axial_chunk_size,
        border_size=border_size
    )

    # Process each chunk
    for start_idx, end_idx, valid_start, valid_end, out_start, out_size in tqdm(chunk_iterator):
        # Extract and process chunk
        chunk = img[..., start_idx:end_idx, -n_frames_linear_regression:]
        
        # Apply Gaussian filter if needed
        if gaussian_filter_size > 0:
            chunk = gaussian_filter(chunk, sigma=[gaussian_filter_size, gaussian_filter_size, gaussian_filter_size, 0])
        
        # Process only the valid portion
        valid_chunk = chunk[..., valid_start:valid_end, :]
        slopes, intercepts = _voxel_patlak_chunk(valid_chunk, input_fun, t, n_frames_linear_regression)
        
        # Store results
        out[..., out_start:out_start + out_size] = slopes
    
    return out