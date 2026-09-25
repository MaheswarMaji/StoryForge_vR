import torch, soundfile as sf
from kokoro import KPipeline

print("CUDA available:", torch.cuda.is_available())

pipe = KPipeline(lang_code='a')   # 'a' = US English, 'h' = Hindi
for i, (_, _, audio) in enumerate(pipe("Hello from Kokoro.", voice='af_heart')):
    sf.write(f'kokoro_{i}.wav', audio, 24000)
