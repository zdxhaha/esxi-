# esxi-
用来监控esxi及vsphere集群的一个python软件/A Python software used to monitor ESXi and vSphere clusters 
    这是用python代码写的一个用来监控esxi平台的一个监控软件，由于以前用vsphere client用的舒服了，每次点击就能够看到esxi平台的所有信息，后来esxi不提供客户端，只能够通过web页面来管理，用起来其实也还不错，但是总觉得差点事，就想找个玩意来替代一下之前的vsphere client ，这就是这个代码的由来；
    使用起来很简单，本地搭建好python的环境之后，运行esxi_monitor.py
    <img width="919" height="689" alt="image" src="https://github.com/user-attachments/assets/d7b5adb0-de23-463b-b9c9-6812d9e78f04" />
按照真实的情况进行填写就可以了！
    目前这些功能我觉得还不错，后续会持续更新新功能，包括ui界面，虽丑，但能用，hahaha
    下边是截图：
概览功能：
<img width="1081" height="709" alt="image" src="https://github.com/user-attachments/assets/38a9d7c5-f522-4790-b08b-44e7c4b740b3" />
健康状况看不到，后边抽空再看看，估计是需要一些硬件，如果不行我后边就删掉它
主机资源功能和概览内容有点重复的感觉，后边再看看取舍
<img width="1092" height="725" alt="image" src="https://github.com/user-attachments/assets/73ae7652-97f2-401a-add9-af6c912d61c0" />
虚拟机：目前基本上显示功能都没有问题，也可以正常的开机，关机，重启等等功能都没有问题，但是控制台功查看还是只能用web页面；
另外，安装了vmtools还能够看到ip地址，系统信息等
<img width="1224" height="652" alt="image" src="https://github.com/user-attachments/assets/ec295d97-f260-41af-ad15-b7f79f382bd9" />
最后一个是存储的情况，当然我这个地方是差不多满了，毕竟虚拟机比较多
<img width="1223" height="309" alt="image" src="https://github.com/user-attachments/assets/c9dd4a20-3ad4-419b-9b4d-d90b8064623b" />
欢迎大家加入群里提提意见，我后续再进行其他的调整：QQ群：913657118
