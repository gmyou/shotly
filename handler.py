def stHandler(key, userid, command):
    cmd = "python st -A https://ssproxy.ucloudbiz.olleh.com/auth/v1.0 -K "+key+" -U "+userid+" delete "+command+" %s"
    
    f = open('upload.list')
    
    for l in f:
        if (l.endswith('\n', 0, 2)):
            pass
        else:
            l = l.replace('\n', '')
            print (cmd % l)
            
stHandler('[YOUR KEY]', '[YOUR ID]', '[list/upload/download/delete]')