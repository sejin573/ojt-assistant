@echo off
rem 전체 화면용 로컬 서버 진입점. py 런처가 없으면 python 명령으로 한 번만 재시도한다.
chcp 65001 >nul
cd /d "%~dp0"
title OJT 작성 도우미
py server.py
if errorlevel 1 python server.py
