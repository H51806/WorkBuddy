@echo off
set GOROOT=D:\多账号管理器\go
set GOPATH=D:\多账号管理器\WorkBuddy_统一管理器\gopath
set GOCACHE=D:\多账号管理器\WorkBuddy_统一管理器\gocache
set GOPROXY=https://goproxy.cn,direct
set PATH=%GOROOT%\bin;%GOPATH%\bin;%PATH%

cd /d D:\多账号管理器\workbuddy2api-panel-main
call %GOROOT%\bin\go.exe mod download
call %GOROOT%\bin\go.exe build -o "D:\多账号管理器\WorkBuddy_统一管理器\wb2api-compiled.exe" ./cmd/server/main.go
