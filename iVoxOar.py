import numpy
import os
import time
import subprocess
import random
import glob
from PyMca import SixCircle
from PyMca import specfilewrapper
import EdfFile
import getconfig


import matplotlib.pyplot as pyplot
import matplotlib.colors

import multiprocessing
import cPickle as pickle
import itertools
import sys
import gzip
import argparse


# wishlist:
# - UB matrix in zapline (spec)
# - correct for mon from spec
# - correct for mismatch in theta for backward/forward scans
# - HKL limits determination
# - fully autonomous distributed calculations on Oar cluster (we need working opid03 account on Nice)
# - detector distance / pixel angular size
# - better control interface
# - patch arbitrary scans
# - possibly: patch onto 2theta


# example usage on Oar:
# ssh to onderwaa@nice
# cd to iVoxOar
# ./blisspython /data/id03/inhouse/2012/Sep12/si2515/iVox/iVoxOar.py cluster -o OUTPUT.zpi /data/id03/inhouse/2012/Sep12/si2515/iVox/test.spec 217 287
# to plot, run (not on nice): python iVoxOar.py plot file.zpi


class Space(object):
    def __init__(self, set):
        self.set = set
        minH, maxH = set['minH'], set['maxH']
        minK, maxK = set['minK'], set['maxK']
        minL, maxL = set['minL'], set['maxL']
        Hres, Kres, Lres = set['Hres'], set['Kres'], set['Lres']

        self.Hcount = int(round((maxH-minH)/Hres))+1
        self.Kcount = int(round((maxK-minK)/Kres))+1
        self.Lcount = int(round((maxL-minL)/Lres))+1

        self.photons = numpy.zeros(self.Hcount*self.Kcount*self.Lcount)
        self.contributions = numpy.zeros(self.photons.shape, dtype=numpy.uint32)

    def __call__(self):
        return numpy.ma.array(data=self.photons/self.contributions, mask=(self.contributions == 0)).reshape((self.Hcount, self.Kcount, self.Lcount), order='C')

    def fill(self, im):
        self.photons[:im.photons.size] += im.photons
        self.contributions[:im.contributions.size] += im.contributions
        
    def __iadd__(self, other):
        if not isinstance(other, Space):
            return NotImplemented
        if self.set != other.set:
            raise ValueError('cannot add spaces with different H/K/L range or resolution')
        self.photons += other.photons
        self.contributions += other.contributions
        return self

class Box(object):
    def __init__(self,set,H,K,L,Intensity):
        minH, maxH = set['minH'], set['maxH']
        minK, maxK = set['minK'], set['maxK']
        minL, maxL = set['minL'], set['maxL']
        Hres, Kres, Lres = set['Hres'], set['Kres'], set['Lres']

        Kcount = int(round((maxK-minK)/Kres))+1
        Lcount = int(round((maxL-minL)/Lres))+1
        
        hkl = (numpy.round((H-minH)/Hres)*Kcount*Lcount + numpy.round((K-minK)/Kres)*Lcount + numpy.round((L-minL)/Lres)).astype(int)

        self.photons = numpy.bincount(hkl.flatten(), weights=Intensity.flatten())
        self.contributions = numpy.bincount(hkl.flatten())

    def __call__(self):
        return numpy.ma.array(data=self.photons/self.contributions, mask=(self.contributions == 0))


class NotAZaplineError(Exception):
        pass


class Arc(object):
    def __init__(self,spec,scanno,set):
        scan = spec.select('{0}.1'.format(scanno))
        self.set = set
        if not scan.header('S')[0].split()[2] == 'zapline':
            raise NotAZaplineError

        scanheaderC = scan.header('C')
        folder = scanheaderC[0].split(' ')[-1].split('/')[-1]
        self.imagefolder = os.path.join(set['imagefolder'],folder)
        self.scannumber = int(scanheaderC[2].split(' ')[-1])
        self.imagenumber = int(scanheaderC[3].split(' ')[-1])
        self.buildfilelist()
        self.edf = EdfFile.EdfFile(self.filelist[0])
        self.delt,self.theta,self.chi,self.phi,self.mu,self.gam = numpy.array(scan.header('P')[0].split(' ')[1:7],dtype=numpy.float)
        if scanno < 405:
            self.UB = numpy.array([2.628602629,0.2730763688,-0.001032444885,1.202301748,2.877587966,-0.001081570571,0.002600281749,0.002198663001,1.54377945])
        else:
            self.UB = numpy.array([2.624469378,0.2632191474,-0.001028869827,1.211297551,2.878506363,-0.001084906521,0.002600359765,0.002198324744,1.54377945])
        self.wavelength = 0.6888074966
        self.theta = scan.data()[0,:]
        self.length = numpy.alen(self.theta)
            
    def buildfilelist(self):
        allfiles =  glob.glob(os.path.join(self.imagefolder,'*si2515_mpx*'))
        filelist = list()
        imagedict = {}
        for file in allfiles:        
            filename = os.path.basename(file).split('.')[0]
            scanno, pointno, imageno = filename.split('_')[-3:]
            scanno, pointno, imageno = int(scanno), int(pointno), int(imageno)
            if not scanno in imagedict:
                imagedict[scanno] = {}
            imagedict[scanno][pointno] = file
        filedict = imagedict[self.scannumber]
        points = sorted(filedict.iterkeys())
        self.filelist = [filedict[i] for i in points]
        if len(self.filelist) == 0:
            raise NameError('Empty filelist, check if the specified imagefolder corresponds to the location of the images')
        
    def getImdata(self,n):
        ymask = numpy.asarray(range(160, 256) + range(262, 400))
        xmask = numpy.arange(40, 255)

        self.data = self.edf.GetData(n)
        app = [0.003125, 0.003125] #angle per pixel (delta,gamma)
        centralpixel = [314,160] #(row,column)=(delta,gamma)
        self.gamma = app[1]*(numpy.arange(self.data.shape[1])-centralpixel[1])+self.gam
        self.delta = app[0]*(numpy.arange(self.data.shape[0])-centralpixel[0])+self.delt
        self.gamma = self.gamma[ymask]
        self.delta = self.delta[xmask]

        R = SixCircle.getHKL(self.wavelength, self.UB, delta=self.delta, theta=self.theta[n],chi=self.chi,phi=self.phi,mu=self.mu,gamma=self.gamma)
#        R.shape = 3,numpy.alen(self.gamma),mp.alen(self.delta)
        H = R[0,:]
        K = R[1,:]
        L = R[2,:]
        roi = self.data[ymask, :]
        roi = roi[:,xmask]
        intensity = roi.flatten()
        return H,K,L, intensity

def process(scanno):
    mesh = Space(set)
    try:
        a = Arc(spec, scanno,set)
    except NotAZaplineError:
        return None
    print scanno
    for m in range(a.length):
        H,K,L, intensity = a.getImdata(m)
        b = Box(set, H,K,L, intensity)
        mesh.fill(b)
    return mesh

def makemesh(firstscan, lastscan):
    scanlist = range(firstscan, lastscan+1)
    globalmesh = Space(set)

    if USE_MULTIPROCESSING:
        iter = pool.imap_unordered(process, scanlist, 1)
    else:
        iter = itertools.imap(process, scanlist)

    for result in iter:
        if result is not None:
            globalmesh += result
    m = globalmesh()
    pickle.dump(m, open('mesh-{0}-{1}.pickle'.format(firstscan, lastscan), 'w'), pickle.HIGHEST_PROTOCOL)


def makeplot(space, dest=None):
    clipping = 0.02
   
    mesh = space() 
    data = numpy.log(mesh[...,0])
    compresseddata = data.compressed()
    chop = int(round(compresseddata.size * clipping))
    clip = sorted(compresseddata)[chop:-chop]
    vmin, vmax = clip[0], clip[-1]
    
    invmask = ~data.mask
    hlims = numpy.flatnonzero(invmask.sum(axis=1))
    hlims = hlims.min()*space.set['Hres'] + space.set['minH'] - 0.1, hlims.max()*space.set['Hres'] + space.set['minH'] + 0.1
    klims = numpy.flatnonzero(invmask.sum(axis=0))
    klims = klims.min()*space.set['Kres'] + space.set['minK'] - 0.1, klims.max()*space.set['Kres'] + space.set['minK'] + 0.1
    
    pyplot.figure(figsize=(12,9))
    #pyplot.imshow(space.contributions.reshape((space.Hcount, space.Kcount, space.Lcount), order='C')[:,:,0].transpose(), origin='lower', extent=(space.set['minH'], space.set['maxH'], space.set['minK'], space.set['maxK']), aspect='auto')#, norm=matplotlib.colors.Normalize(vmin, vmax))

    pyplot.imshow(data.transpose(), origin='lower', extent=(space.set['minH'], space.set['maxH'], space.set['minK'], space.set['maxK']), aspect='auto', norm=matplotlib.colors.Normalize(vmin, vmax))
    pyplot.xlabel('H')
    pyplot.ylabel('K')
    #pyplot.suptitle() TODO
    pyplot.colorbar()
    pyplot.xlim(*hlims)
    pyplot.ylim(*klims)
    if dest:
        pyplot.savefig(dest)
    else:
        pyplot.show()


if __name__ == "__main__":
    def run(*command):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output, unused_err = process.communicate()
        retcode = process.poll()
        return retcode, output

    def oarsub(*args):
        scriptname = './blisspython /data/id03/inhouse/2012/Sep12/si2515/iVoxOar/iVoxOar.py '
        command = '{0} {1}'.format(scriptname, ' '.join(args))
        ret, output = run('oarsub', command)
        if ret == 0:
            lines = output.split('\n')
            for line in lines:
                if line.startswith('OAR_JOB_ID='):
                    void, jobid = line.split('=')
                    return jobid
        return False

    def oarstat(jobid):
# % oarstat -s -j 5651374
# 5651374: Running
# % oarstat -s -j 5651374
# 5651374: Finishing
        ret, output = run('oarstat', '-s', '-j', str(jobid))
        if ret == 0:
            job, status = output.split(':')
            return status.strip()
        else:
            return 'Unknown'

    def oarwait(jobs):
        while jobs:
            status = oarstat(jobs[0])
            if status == 'Running' or status == 'Waiting' or status == 'Unknown':
                time.sleep(5)
            else: # assume status == 'Finishing' or 'Terminated' but don't wait on something unknown
                jobs.pop(0)

    def cluster(args):
        prefix = 'iVoxOar-{0:x}'.format(random.randint(0, 2**32-1)) 
        
        jobs = []
        parts = []
        for scanno in range(args.firstscan, args.lastscan+1):
            part = '{0}/{1}-part-{2}.zpi'.format(args.tmpdir, prefix, scanno)
            jobs.append(oarsub('--hkres', str(args.hkres), '--lres', str(args.lres), '_part', set['specfile'], str(scanno), '-o', part))
            parts.append(part)
        print 'submitted {0} jobs, waiting...'.format(len(jobs))
        oarwait(jobs)

        count = args.lastscan - args.firstscan + 1
        chunkcount = int(numpy.ceil(float(count) / args.chunksize))
        if chunkcount == 1:
            job = oarsub('_sum', '--delete', '-o', args.outfile, *parts)
            print 'submitted final job, waiting...'
            oarwait([job])
        else:
            chunksize = int(numpy.ceil(float(count) / chunkcount))
            jobs = []
            chunks = []
            for i in range(chunkcount):
                chunk = '{0}/{1}-chunk-{2}.zpi'.format(args.tmpdir, prefix, i+1)
                jobs.append(oarsub('_sum', '--delete', '-o', chunk, *parts[i*chunksize:(i+1)*chunksize]))
                chunks.append(chunk)
            print 'submitted {0} jobs, waiting...'.format(len(jobs))
            oarwait(jobs)

            job = oarsub('_sum', '--delete', '-o', args.outfile, *chunks)
            print 'submitted final job, waiting...'
            oarwait([job])
        print 'done!'

    def part(args):
        global spec
        spec = specfilewrapper.Specfile(set['specfile'])
        space = process(args.scan)
        pickle.dump(space, gzip.GzipFile(fileobj=args.outfile), pickle.HIGHEST_PROTOCOL)

    def sum(args):
        globalspace = Space(set)
        for fn in args.infiles:
            print fn
            result = pickle.load(gzip.open(fn))
            if result is not None:
                globalspace += result
        pickle.dump(globalspace, gzip.GzipFile(fileobj=args.outfile), pickle.HIGHEST_PROTOCOL)
        if args.delete:
            for fn in args.infiles:
                try:
                    os.remove(fn)
                except:
                    pass

    def local(args):
        global spec
        spec = specfilewrapper.Specfile(set['specfile'])

#        set['Hres'] = set['Kres'] = args.hkres
#        set['Lres'] = args.lres

        scanlist = range(args.firstscan, args.lastscan+1)
        globalspace = Space(set)
     
        if args.multiprocessing:
            pool = multiprocessing.Pool()
            iter = pool.imap_unordered(process, scanlist, 1)
        else:
            iter = itertools.imap(process, scanlist)
     
        for result in iter:
            if result is not None:
                globalspace += result

#        pickle.dump(globalspace, gzip.GzipFile(fileobj=set['outputfilename'],mode = 'w'), pickle.HIGHEST_PROTOCOL)
        pickle.dump(globalspace, gzip.open(set['outputfilename'],'w+'), pickle.HIGHEST_PROTOCOL)
                    

        if args.plot:
            if args.plot is True:
                makeplot(globalspace, None)
            else:
                makeplot(globalspace, args.plot)

    def plot(args):
        space = pickle.load(gzip.GzipFile(set['outputfilename']))
        print space
        makeplot(space, args.outfile)

    def test(args):
        print set
        print 'So far so good...'
    
    parser = argparse.ArgumentParser(prog='iVoxOar')
    parser.add_argument('--config',default='./config')
    parser.add_argument('--hkres', type=float, default=0.001)
    parser.add_argument('--lres', type=float, default=1.)
    subparsers = parser.add_subparsers()

    parser_cluster = subparsers.add_parser('cluster')
    parser_cluster.add_argument('specfile')
    parser_cluster.add_argument('firstscan', type=int)
    parser_cluster.add_argument('lastscan', type=int)
    parser_cluster.add_argument('-o', '--outfile', required=True)
    parser_cluster.add_argument('--tmpdir', default='.')
    parser_cluster.add_argument('--chunksize', default=20, type=int)
    parser_cluster.set_defaults(func=cluster)

    parser_part = subparsers.add_parser('_part')
    parser_part.add_argument('specfile')
    parser_part.add_argument('scan', type=int)
    parser_part.add_argument('-o', '--outfile', type=argparse.FileType('wb'), required=True)
    parser_part.set_defaults(func=part)
    
    parser_sum = subparsers.add_parser('_sum')
    parser_sum.add_argument('-o', '--outfile', type=argparse.FileType('wb'), required=True)
    parser_sum.add_argument('--delete', action='store_true')
    parser_sum.add_argument('infiles', nargs='+')
    parser_sum.set_defaults(func=sum)

    parser_local = subparsers.add_parser('local')
#    parser_local.add_argument('specfile')
    parser_local.add_argument('firstscan', type=int)
    parser_local.add_argument('lastscan', type=int)
#    parser_local.add_argument('-o', '--outfile', type=argparse.FileType('wb'), required=True)
    parser_local.add_argument('-p', '--plot', nargs='?', const=True)
    parser_local.add_argument('-m', '--multiprocessing', action='store_true')
    parser_local.set_defaults(func=local)

    parser_plot = subparsers.add_parser('plot')
#    parser_plot.add_argument('infile', type=argparse.FileType('rb'))
    parser_plot.add_argument('outfile', nargs='?')
    parser_plot.set_defaults(func=plot)

    parser_plot = subparsers.add_parser('test')
    parser_plot.set_defaults(func=test)

    args = parser.parse_args()
    
    set = getconfig.configdict(args.config)
    
    args.func(args)
