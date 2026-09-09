import os, json, re

wav_dir = 'logs/madoka_0908'
asr_file = 'logs/madoka_0908/asr_results.json'

with open(asr_file, 'r', encoding='utf-8') as f:
    asr_results = json.load(f)

train_lines = []
for item in asr_results:
    wav_name = item['wav']
    text = item['text']
    
    if len(text) < 2:
        continue
    if re.search(r'[\uac00-\ud7af]', text):
        continue
    if re.match(r'^[.!?、。！？\s]+$', text):
        continue
    
    wav_path = os.path.abspath(os.path.join(wav_dir, wav_name))
    wav_path = wav_path.replace('\\', '/')
    line = f'{wav_path}|madoka|ZH|{text}'
    train_lines.append(line)
    print(f'{wav_name}: {text[:60]}')

print(f'\nTotal valid lines: {len(train_lines)}')

with open('logs/madoka_0908/train.list', 'w', encoding='utf-8') as f:
    for line in train_lines:
        f.write(line + '\n')
