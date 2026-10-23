import json,numpy as np,h5py
ARRAYS=('s11_linear','s22_linear','s12_real','s12_imag')
SCALARS=('start_utc_us','end_utc_us','timestamps_us','frame_ids_first','frame_ids_last','valid_fft_frames','sample_start','sample_end','backend_timestamp_first_us','backend_timestamp_last_us','integration_duration_sec','coverage_sec','cadence_sec','quality_flags')
def text(v):return v.decode() if isinstance(v,bytes) else str(v)
def validate(f):
    meta=f['metadata'].attrs
    if int(meta['data_schema_version'])!=1:raise ValueError('unsupported data_schema_version')
    if text(meta['units'])!='ADC-count^2 per input FFT bin (mean after frequency grouping)':raise ValueError('unsupported units')
    if text(meta['cross_product_convention'])!='X1*conj(X2) * exp(-j*phi)':raise ValueError('unsupported cross-product convention')
    n=len(f['data/timestamps_us']);bins=len(f['frequency_hz'])
    if not n or not bins:raise ValueError('empty finalized observation')
    for name in ARRAYS:
        if f['data/'+name].shape!=(n,bins):raise ValueError('inconsistent HDF5 dimensions')
    for name in SCALARS:
        if f['data/'+name].shape!=(n,):raise ValueError('inconsistent scalar dimensions')
    freq=f['frequency_hz'][:]
    if not np.all(np.isfinite(freq)) or not np.all(np.diff(freq)>0):raise ValueError('invalid RF axis')
    return n,bins
def open_final(path):
    if not str(path).endswith('.h5'):raise ValueError('only finalized .h5 is readable')
    f=h5py.File(path,'r')
    try:validate(f)
    except BaseException:f.close();raise
    return f
def snapshot(f):return json.loads(text(f['metadata'].attrs['effective_config_json']))
