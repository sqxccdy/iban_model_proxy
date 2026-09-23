import requests

url = "https://chat.deepseek.com/api/v0/chat/completion"

# 必须从浏览器复制以下两个值（F12 -> Network 抓包）
headers = {
    "Authorization": "Bearer MBfq39K7N98YjWpx4KXrtoI3RgMDR6G80/fY2I3cNhlyNxa5bQxEVDwtDXLb+OYJ",
    "Cookie": "HWWAFSESTIME=1786589307136; HWWAFSESID=7688a1b10ebc93abf70e; smidV2=20260813104831e54314b63f7835a453dcf689155ee326002709a8bb2ff46a0; .thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae=lk3P0xzKBn0JGVJVWeeTJvpWM6N5jcAh3v9VXh7Wdic+jiqlYElO4gObDR1TIwa/8gJlHx0095UFSl0Q/GSbYw%3D%3D; ds_session_id=f9a46465d3cb43339073589887ba0add",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Origin": "https://chat.deepseek.com",
    "referer": "https://chat.deepseek.com/a/chat/s/161d39e3-51ae-4f02-81bf-4d186bb96126",
    "x-ds-pow-response": "eyJhbGdvcml0aG0iOiJEZWVwU2Vla0hhc2hWMSIsImNoYWxsZW5nZSI6ImVjYmIyMzJhNmVjZDYzMjBjZDg3MmQ1OGQyMGE3Y2ZkMjYxZDk3Mjg0ZTU1NTI1ZjRhYmUwN2Y5MjlmN2I3NWMiLCJzYWx0IjoiYjYyMzNkZWMwZTc2ZmI3OGE2M2YiLCJhbnN3ZXIiOjU0NDYwLCJzaWduYXR1cmUiOiJjMjc3NmNlOTc1NGE2MjU5MDMzMmNmODhjNjA3ZDNiYmY0ZmY3ZWY2NjE1MjQwNzk5MzliMTY0ZjIxZjg4YTNmIiwidGFyZ2V0X3BhdGgiOiIvYXBpL3YwL2NoYXQvY29tcGxldGlvbiJ9",
    "x-hif-leim": "g/2FhHuQ/xFpsWgUki7sALpWCxenPI7KzqyNbKf+XTZKlRtW/RTmQKA=.T4QzusSLeO2OhkfF",
}

payload = {
    "chat_session_id": "161d39e3-51ae-4f02-81bf-4d186bb96126",
    "parent_message_id": 2,
    "model_type": None,
    "prompt": "你好",
    "ref_file_ids": [],
    "thinking_enabled": True,
    "search_enabled": True,
    "action": None,
    "preempt": False
}

resp = requests.post(url, headers=headers, json=payload)
print(resp.json())  # 打印完整 JSON 响应
# 提取回复内容:
# print(resp.json()["choices"][0]["message"]["content"])
