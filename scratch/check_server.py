from openadapt_grounding.deploy.deploy import Deploy
output = Deploy._run_ssh_command("grep -n reload /app/OmniParser/omnitool/omniparserserver/omniparserserver.py")
print(output)
