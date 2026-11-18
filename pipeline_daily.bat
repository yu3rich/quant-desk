@echo off
rem QuantDesk 手动补跑数据链：行情增量 -> 快照 -> 训练集（模型按月才重训）
rem
rem 平时不需要它 —— 工作台自带定时（选股设置 → 数据准备 → 「每交易日自动更新」，
rem 默认 18:10，baostock 日线入库之后跑）。只有服务长时间没开、想脱离界面
rem 手动补一次数据时，双击本文件即可。
cd /d "%~dp0"
if not exist "data" mkdir "data"
echo [%date% %time%] pipeline start >> data\pipeline_last.log
".venv\Scripts\python.exe" pipeline.py --auto >> data\pipeline_last.log 2>&1
echo [%date% %time%] pipeline done >> data\pipeline_last.log
