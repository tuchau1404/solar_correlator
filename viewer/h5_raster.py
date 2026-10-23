"""Read-only waterfall raster with transparent invalid cells and exact row edges.

One RGBA image per continuous segment; each image row is painted onto its
saved UTC support. No interpolation across rows, segments or missing cells.
"""
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui


class TimeRasterItem(pg.GraphicsObject):
    def __init__(self, frequency_edges, time_edges, values, levels, color_map):
        super().__init__()
        self.edges = np.asarray(time_edges, dtype=float)
        fe = np.asarray(frequency_edges, dtype=float)
        values = np.asarray(values, dtype=float)
        if values.shape != (len(self.edges)-1, len(fe)-1):
            raise ValueError('Raster dimensions do not match edges')
        if not np.all(np.diff(self.edges)>0) or not np.all(np.diff(fe)>0):
            raise ValueError('Raster edges must increase')
        self._rect = QtCore.QRectF(float(fe[0]),float(self.edges[0]),
                                  float(fe[-1]-fe[0]),float(self.edges[-1]-self.edges[0]))
        lo,hi = map(float,levels)
        if not np.isfinite(lo+hi) or hi<=lo:
            raise ValueError('Color levels must be finite and increasing')
        valid = np.isfinite(values)
        # Only the color lookup receives placeholders. Invalid measurement cells
        # remain invalid and their alpha is zero, never presented as numeric zero.
        scaled = np.zeros(values.shape,dtype=float)
        scaled[valid] = np.clip((values[valid]-lo)/(hi-lo),0,1)
        lut = color_map.getLookupTable(0,1,256,alpha=True)
        rgba = np.ascontiguousarray(lut[np.rint(scaled*255).astype(np.uint8)],dtype=np.uint8)
        rgba[~valid,3]=0
        self.invalid_cells = int((~valid).sum())
        height,width = values.shape
        self.image = QtGui.QImage(rgba.data,width,height,rgba.strides[0],QtGui.QImage.Format_RGBA8888).copy()

    def boundingRect(self):
        return self._rect

    def paint(self,painter,option,widget=None):
        painter.save()
        try:
            painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform,False)
            exposed = option.exposedRect
            for row,(start,end) in enumerate(zip(self.edges[:-1],self.edges[1:])):
                target = QtCore.QRectF(self._rect.left(),float(start),self._rect.width(),float(end-start))
                if target.intersects(exposed):
                    painter.drawImage(target,self.image,QtCore.QRectF(0,row,self.image.width(),1))
        finally:
            painter.restore()
