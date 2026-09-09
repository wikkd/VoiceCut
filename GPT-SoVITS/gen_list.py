import json
import re

with open('D:/projects/ai-agent-test/GPT-SoVITS/logs/madoka/asr_output/asr_results_sensevoice.json', 'r', encoding='utf-8') as f:
    results = json.load(f)

valid = [r for r in results if r['text'].strip()]
print(f'Total: {len(results)}, Valid: {len(valid)}')

output_list = 'D:/projects/ai-agent-test/GPT-SoVITS/logs/madoka/madoka_train.list'
with open(output_list, 'w', encoding='utf-8') as f:
    for r in results:
        if r['text'].strip():
            wav_path = r['wav_path'].replace('\\', '/')
            # SenseVoice 输出包含 <|ja|> 等标签，需要清理
            text = r['text']
            # 移除 SenseVoice 特殊标签
            text = re.sub(r'<\|[^|]*\|>', '', text).strip()
            f.write(f"{wav_path}|madoka|ja|{text}\n")

print(f'Generated {output_list}')

print('\nSample entries:')
for r in valid[:5]:
    text = re.sub(r'<\|[^|]*\|>', '', r['text']).strip()
    print(f"  {r['wav_file']}: {text[:80]}")
