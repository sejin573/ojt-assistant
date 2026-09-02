@echo off
chcp 65001 >nul
cd /d "%~dp0"
title OJT 작성 도우미
py server.py
if errorlevel 1 python server.py
