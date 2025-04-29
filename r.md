git submodule add https://github.com/vimson999/KS-Downloader-v1.git submodules/ks_downloader

pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

pip install -r requirements.txt
pip install -r submodules/ks_downloader/requirements.txt


nohup pip install -r requirements.txt > install.log 2>&1 &
tail -f install.log

tree -L 10 -I 'venv|__pycache__|node_modules|.git|.idea|.vscode|static|dist|logs|tmp|.env|docs'


uvicorn app.main:app --reload --port 9000

curl -X POST "http://127.0.0.1:9000/info" -H "Content-Type: application/json" -d '{"url": "https://www.kuaishou.com/f/X-MGnYq0BTJfH0y"}'

curl -X POST "http://127.0.0.1:9000/info" -H "Content-Type: application/json" -d '{"url": "https://www.kuaishou.com/f/X34s4ikwqfv7vZN"}'


