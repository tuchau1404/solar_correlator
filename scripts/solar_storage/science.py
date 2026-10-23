"""Pure array science, no plotting, filesystem or networking."""
import numpy as np
def derived(a,b,re,im):
    valid=np.isfinite(a)&np.isfinite(b)&np.isfinite(re)&np.isfinite(im)&(a>=0)&(b>=0)
    mag=np.where(valid,np.hypot(re,im),np.nan)
    phase=np.where(valid&(mag>0),np.arctan2(im,re),np.nan)
    with np.errstate(divide='ignore',invalid='ignore',over='ignore'):
        gamma=np.where(valid&(a>0)&(b>0),(mag/np.sqrt(a))/np.sqrt(b),np.nan)
    violation=np.isfinite(gamma)&(gamma>1+32*np.finfo(np.float32).eps)
    gamma=np.where(violation,np.nan,np.minimum(gamma,1));phase=np.where(violation,np.nan,phase)
    return mag,phase,gamma,gamma*gamma,violation
def db(power):
    with np.errstate(divide='ignore',invalid='ignore'):return np.where(power>0,10*np.log10(power),np.nan)
def weighted_mean(values,weights):
    weights=np.broadcast_to(np.asarray(weights)[:,None],values.shape)
    mask=np.isfinite(values)&np.isfinite(weights)&(weights>0)
    numerator=np.sum(np.where(mask,values*weights,0),axis=0)
    denominator=np.sum(np.where(mask,weights,0),axis=0)
    with np.errstate(divide='ignore',invalid='ignore'):return np.where(denominator>0,numerator/denominator,np.nan)
