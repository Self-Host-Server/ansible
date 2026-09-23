.PHONY: update bootstrap-host bootstrap-all laptop-all containers-host bootstrap-nodes proxmox-update vault-edit

site:=ansible-playbook site.yml --tags
notify:=./run-update.sh site.yml --tags
limit:=--limit $(HOST)
# bootstrap targets stay on the plain (non-wrapped) command: they're
# interactive (--ask-pass/--ask-become-pass), and run-update.sh redirects the
# subprocess's stdout to a logfile, which would hide the password prompts.
bootstrap:=$(site) setup --ask-pass --ask-become-pass
bootstrap-nodes-cmd:=$(site) bootstrap-nodes --ask-pass
containers:=$(notify) containers
proxmox_nodes_cmd:=$(notify) proxmox-nodes
conda_name:=ansible
run:=conda run -n $(conda_name)

update:
	make laptop-update
	make containers-all

bootstrap-host:
	$(bootstrap) $(limit)

bootstrap-all:
	$(bootstrap)

bootstrap-nodes:
	$(bootstrap-nodes-cmd)

proxmox-update:
	$(proxmox_nodes_cmd)

vault-edit:
	EDITOR=nano ansible-vault edit playbooks/group_vars/all/vault.yml

laptop-update:
	$(notify) laptop

containers-all:
	$(containers)

containers-host:
	$(containers) $(limit)

conda:
	conda env create -f environment.yml -n $(conda_name)
	make node

node:
	$(run) nodeenv -p

npm:
	make node
	$(run) npm install
	$(run) npm update
