"""Count every sample; rebin the full data only when the range expands."""
import numpy as np


class Histogram:
    def __init__(self,bins=60):
        self.bins=bins;self.values=[];self.pending=[];self.edges=None;self.counts=np.zeros(bins,dtype=int)

    def add(self,values):
        batch=list(values);self.values.extend(batch);self.pending.extend(batch)

    def update(self):
        if not self.pending:return self.edges,self.counts
        values=np.asarray(self.pending,dtype=float);self.pending=[]
        low,high=float(values.min()),float(values.max())
        if self.edges is None or low<self.edges[0] or high>self.edges[-1]:
            all_values=np.asarray(self.values,dtype=float)
            low,high=float(all_values.min()),float(all_values.max())
            padding=max((high-low)*.12,max(abs(low),abs(high))*1e-6,1e-15)
            self.edges=np.linspace(low-padding,high+padding,self.bins+1)
            self.counts=np.histogram(all_values,bins=self.edges)[0]
        else:self.counts+=np.histogram(values,bins=self.edges)[0]
        return self.edges,self.counts
