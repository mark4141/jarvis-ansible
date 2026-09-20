# jarvis-ansible

Ansible for Ubuntu Server on jarvis.

Default: **Bambuddy only**. Pi-hole and Nextcloud are optional tags.

## On jarvis

```bash
sudo apt update
sudo apt install -y ansible git
ansible-galaxy collection install -r requirements.yml

git clone https://github.com/mark4141/jarvis-ansible.git
cd jarvis-ansible

echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/$USER
sudo chmod 440 /etc/sudoers.d/$USER

ansible-playbook site.yml --tags bambuddy
```

Open http://192.168.2.43:8000
