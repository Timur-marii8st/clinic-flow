"""Build fixed Russian announcement clips locally using eSpeak NG.

No patient data, network services or runtime TTS. Generated WAVs are build
artifacts; regenerate them when changing the voice, then test on hardware.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import wave

ROOT=Path(__file__).resolve().parents[1]
PHRASES={'intro':'Талон А','room':'кабинет','generic':'Пройдите в указанный кабинет',
         'ready':'Озвучка включена'}
PHRASES.update(dict(zip('0123456789',['ноль','один','два','три','четыре','пять','шесть','семь','восемь','девять'])))


def build(output,voice='ru'):
    binary=shutil.which('espeak-ng')
    if not binary:
        raise RuntimeError('Install espeak-ng before building offline audio')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    for name,text in PHRASES.items():
        temporary=output/(name+'.tmp.wav')
        subprocess.run([binary,'-v',voice,'-s','145','-w',str(temporary),text],check=True,timeout=20)
        with wave.open(str(temporary),'rb') as wav:
            if wav.getnframes()==0 or wav.getnchannels()!=1:
                raise RuntimeError('Unexpected audio output')
        temporary.replace(output/(name+'.wav'))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default=ROOT/'app'/'static'/'audio',type=Path)
    p.add_argument('--voice',default='ru')
    args=p.parse_args()
    build(args.output,args.voice)
    print('Generated',len(PHRASES),'local WAV clips')
