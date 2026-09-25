import os
os.environ["COQUI_TOS_AGREED"] = "1"
from TTS.api import TTS

tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cuda")   # "cpu" if no GPU
tts.tts_to_file(text="Hello from XTTS.", speaker_wav="reference.wav",
                language="en", file_path="xtts_out.wav")
