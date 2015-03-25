#!/usr/bin/env python

# Run an ultrasound session.

import os, os.path, sys, signal, subprocess, shutil
import numpy as np
#import win32api, win32con
from datetime import datetime
from dateutil.tz import tzlocal
import Image
import wave
from contextlib import closing
import getopt
import random
import ultratils.disk_streamer
import time
import win32file

# TEMP
#newcmd = 'C:\\build\\ultracomm.6.1.0\\bin\\Debug\\ultracomm.exe'
#oldcmd = 'C:\\bin\\ultracomm.exe'
#if os.path.isfile(newcmd):
#    if os.path.isfile(oldcmd):
#        print "Removing {}.".format(oldcmd)
#        os.remove(oldcmd)
#    print "Renaming {} to {}.".format(newcmd, oldcmd)
#    os.rename(newcmd, oldcmd)
PROJECT_DIR = "C:\\Users\\lingguest\\acq"
RAWEXT = ".bpr"

SYNC_CHAN = 1 # audio channel where synchronization signal is found (zero-based)
NORM_SYNC_THRESH = 0.2  # normalized threshold for detecting synchronizaton signal.
MIN_SYNC_TIME = 0.0005   # minimum time threshold must be exceeded to detect synchronization signal
                         # With pstretch unit sync signals are about 1 ms

standard_usage_str = '''python ultrasession.py --params paramfile [--stims filename] [--ultracomm ultracomm_cmd] [--random] [--no-prompt]'''
help_usage_str = '''python ultrasession.py --help|-h'''

def usage():
    print('\n' + standard_usage_str)
    print('\n' + help_usage_str + '\n')

def help():
    print('''
ultrasession.py: Perform one or more ultrasound acquisitions with the
ultracomm command line utility. Organize output into timestamped folders,
one per acquisition. Postprocess synchronization signal and separate audio
channels into separate speech and synchronization .wav files.
''')
    print('\n' + standard_usage_str)
    print('''
Required arguments:

    --params paramfile
    The name of an parameter file to pass to ultracomm.

Optional arguments:

    --stims stimfile
    The name of a file containing stimuli, one per line. Each stimulus
    line will correspond to one acquisition, and the stimulus line will be
    copied to the file stim.txt in the acquisition subdirectory. If no
    stimulus file is provided then ultrasession will perform a single
    acquisition and stop.

    --ultracomm ultracomm_cmd
    The name of the ultracomm command to use to connect the Ultrasonix,
    including path, if desired. If this option is not provided the script
    will default to 'ultracomm'.

    --random
    When this option is provided stimuli will presented in a
    randomized order. When it is not provided stimuli will be presented they
    appear in the stimulus file.

    --no-prompt
    When this option is provided the operator will not be prompted to
    press the Enter key to start an acquisition. Acquisition will begin
    immediately.
    
''')
    

# From https://github.com/mgeier/python-audio/blob/master/audio-files/utility.py
def pcm2float(sig, dtype=np.float64):
    '''Convert integer pcm data to floating point.'''
    sig = np.asarray(sig)  # make sure it's a NumPy array
    assert sig.dtype.kind == 'i', "'sig' must be an array of signed integers!"
    dtype = np.dtype(dtype)  # allow string input (e.g. 'f')
    # Note that 'min' has a greater (by 1) absolute value than 'max'!
    # Therefore, we use 'min' here to avoid clipping.
    return sig.astype(dtype) / dtype.type(-np.iinfo(sig.dtype).min)

def loadsync(wavfile, chan):
    '''Load synchronization signal from an audio file channel and return as a normalized
(range [-1 1]) 1D numpy array.'''
    with closing(wave.open(wavfile)) as w:
        nchannels = w.getnchannels()
        assert w.getsampwidth() == 2
        data = w.readframes(w.getnframes())
        rate = w.getframerate()
    sig = np.frombuffer(data, dtype='<i2').reshape(-1, nchannels)
    return (pcm2float(sig[:,chan], np.float32), rate)

def sync_pstretch(sig, threshold, min_run):
    '''Find and return indexes of synchronization points from pstretch unit,
defined as the start of a sequence of elements of at least min_run length,
all of which are above threshold.'''
    # Implementation: Create a boolean integer array where data points that
    # exceed the threshold == 1, below == 0, then diff the boolean array.
    # Sequences of true (above threshold) elements start where the diff == 1,
    # and the sequence ends (falls below threshold) where the diff == -1.
    # Circumfixing the array with zero values ensures there will be an equal
    # number of run starts and ends even if the signal starts or ends with
    # values above the threshold.
    # TODO: auto threshold (%age of max?)
    bounded = np.hstack(([0], sig, [0]))
    thresh_sig = (bounded > threshold).astype(int)
    difs = np.diff(thresh_sig)
    run_starts = np.where(difs == 1)[0]
    run_ends = np.where(difs == -1)[0]
    return run_starts[np.where((run_ends - run_starts) > min_run)[0]]

def sync2text(wavname):
    '''Find the synchronization signals in an acquisition's .wav file and
create a text file that contains frame numbers and time stamps for each pulse.'''
    (syncsig, rate) = loadsync(wavname, SYNC_CHAN)
    syncsamp = sync_pstretch(syncsig, NORM_SYNC_THRESH, MIN_SYNC_TIME * rate)
    synctimes = np.round(syncsamp * 1.0 / rate, decimals=4)
    print "Found {0:d} synchronization pulses.".format(len(syncsamp))
    dtimes = np.diff(synctimes)
    print "Frame durations range [{0:1.4f} {1:1.4f}].".format(dtimes.min(), dtimes.max())
    txtname = wavname.replace('.wav', '.sync.txt')
    with open(txtname, 'w') as fout:
        for idx,t in enumerate(synctimes):
            fout.write("{0:0.4f}\t{1:d}\n".format(t,idx))
        
def acquire(acqname, paramsfile, ultracomm_cmd):
    '''Perform a single acquisition, creating output files based on acqname.'''
    # Make sure Ultrasonix is frozen before we start recording.
    frz_args = [ultracomm_cmd, '--params', paramsfile, '--freeze-only']
    frz_proc = subprocess.Popen(frz_args)
    frz_proc.wait()

    ult_args = [ultracomm_cmd, '--params', paramsfile, '--output', acqname, '--named-pipe']

    streamer = ultratils.disk_streamer.DiskStreamer("{}.wav".format(acqname))
    streamer.start_stream()
    ult_proc = subprocess.Popen(ult_args)
    pipename = r'\\.\pipe\ultracomm'
    start = time.time()
    fhandle = None
    while not fhandle:
        try:
            fhandle = win32file.CreateFile(pipename, win32file.GENERIC_WRITE, 0, None, win32file.OPEN_EXISTING, 0, None)
        except:
            time.sleep(0.1)
            fhandle = None
            if time.time() - start > 10:
                raise IOError("Could not connect to named pipe")
    raw_input("Press Enter to end ultrasession.")
    win32file.WriteFile(fhandle, 'END')
    while ult_proc.poll() is None:
        time.sleep(0.01)
    streamer.stop_stream()
    streamer.close()
    #rec_args = ['C:\\bin\\rec.exe', '--no-show-progress', '-c', '2', acqname + '.wav']
    #rec_proc = subprocess.Popen(rec_args, shell=True)
    #ult_proc = subprocess.Popen(ult_args)
    #ult_proc.wait()
    #subprocess.check_call(ult_args)
    # Stop sox by sending Ctrl-C to the console
    #print "***********************************"
    #print "Press Ctrl-C to stop sox recording."
    #print "***********************************"
    #rec_proc.wait()
    #win32api.GenerateConsoleCtrlEvent(win32con.CTRL_C_EVENT, 0)

def separate_channels(acqname):
    '''Separate the left and right channels from the acquisition .wav.'''
    wavname = acqname + '.wav'
    for num in ['1', '2']:
        ch = acqname + '.ch' + num + '.wav'
        sox_args = ['C:\\bin\\sox.exe', wavname, ch, 'remix', num]
        sox_proc = subprocess.Popen(sox_args, shell=True)
        sox_proc.wait()
        if sox_proc.returncode != 0:
            for line in sox_proc.stderr:
                sys.stderr.write(line + '\n')
            raise Exception("sox exited with status: {0}".format(sox_proc.returncode))


if __name__ == '__main__':
    try:
        opts, args = getopt.getopt(sys.argv[1:], "p:s:u:r:h", ["params=", "stims=", "ultracomm=", "random", "help", "no-prompt"])
    except getopt.GetoptError as err:
        print str(err)
        usage()
        sys.exit(2)
    params = None
    stimfile = None
    ultracomm = 'ultracomm'
    randomize = False
    no_prompt = False
    for o, a in opts:
        if o in ("-p", "--params"):
            params = a
        elif o in ("-s", "--stims"):
            stimfile = a
        elif o in ("-u", "--ultracomm"):
            ultracomm = a
        elif o in ("-r", "--random"):
            randomize = True
        elif o in ("-h", "--help"):
            help()
            sys.exit(0)
        elif o == "--no-prompt":
            no_prompt = True
    if params == None:
        usage()
        sys.exit(2)
    stims = []
    if stimfile == None:
        stims = ['']
    else:
        with open(stimfile, 'rb') as file:
            for line in file.readlines():
                stims.append(line.rstrip())
    if randomize:
        random.shuffle(stims)
    for stim in stims:
        if no_prompt is False:
            raw_input("Press <Enter> for acquisition.")
        tstamp = datetime.now(tzlocal()).replace(microsecond=0).isoformat().replace(":","")
        acqdir = os.path.join(PROJECT_DIR, tstamp)
        if not os.path.isdir(acqdir):
            try:
                os.mkdir(acqdir)
            except:
                print "Could not create {%s}!".format(acqdir)
                raise
        try:
            if stim != '':
                print("\n\n******************************\n\n")
                print(stim)
                print("\n\n******************************\n\n")

            acqbase = os.path.join(acqdir, tstamp + RAWEXT)
            acquire(acqbase, params, ultracomm)
            try:
                copyparams = os.path.join(acqdir, 'params.cfg')
                print "Copying ", params, " to ", copyparams
                shutil.copyfile(params, copyparams)
                with open(os.path.join(acqdir, 'stim.txt'), 'w+') as stimout:
                    stimout.write(stim)
            except IOError:
                print "Could not copy parameter file or create stim.txt! ", e
                raise
        except Exception as e:
            print "Error in acquiring! ", e
            raise
    #    try:
    #        print "Converting files"
    #        raw2bmp(acqdir)
    #        print "Done converting"
    #    except KeyboardInterrupt:
    #        pass    # don't stop on Ctrl-C in acquire(). This is a hack.
    #    except Exception as e:
    #        print "Error in converting raw image files to .bmp!", e
    #        raise
    
        try:
            print "Separating audio channels"
            separate_channels(acqbase)
        except Exception as e:
            print "Error in separating audio channels", e
            raise
    
        try:
            print "Creating synchronization textgrid"
            wavname = acqbase + '.wav'
            print "synchronizing ", wavname
            sync2text(wavname)
            print "Created synchronization text file"
        except Exception as e:
            print "Error in creating synchronization textgrid!", e
            raise


