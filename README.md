# ios17性能采集
ios17以上设备简单性能统计脚本

# 使用教程
    pip install -r requirements.txt
```python
"""
    修改main.py中的
    将bundle_id指向需要测试的app名称，udid指向目标设备
    例如：
"""
bundle_id = "com.habby.capybara" # 卡皮巴拉性能，可以通过下面代码获取
device = usbmux.list_devices(usbmux_address=None)
lockdown = create_using_usbmux(device[0].serial, autopair=False, connection_type=device[0].connection_type,
                               usbmux_address=None)
print(InstallationProxyService(lockdown=lockdown).get_apps(application_type="User", calculate_sizes=False))
```
报告生成在项目目录下
