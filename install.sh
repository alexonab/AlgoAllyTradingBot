mv settings.py.default settings.py
sudo yum update -y
sudo yum install python36 git -y
sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install -U aiohttp
sudo python3 -m pip install -U discord.py==0.16.12
sudo python3 -m pip install --upgrade aiohttp
# sudo python3 -m pip install config
git clone https://github.com/joshs85/tastyworks_api.git
cd tastyworks_api
sudo python3 setup.py install
cd ..
sudo cp pptwbot /etc/init.d/pptwbot
sudo chmod +x /etc/init.d/pptwbot
sudo ln -s /etc/rc.d/init.d/pptwbot /etc/rc.d/rc3.d/S99pptwbot
sudo reboot
