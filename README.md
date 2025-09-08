

ERP15 as a monolith includes the following areas for managing businesses:

1. [Accounting](https://erpnext.com/open-source-accounting)
1. [Warehouse Management](https://erpnext.com/distribution/warehouse-management-system)
1. [CRM](https://erpnext.com/open-source-crm)
1. [Sales](https://erpnext.com/open-source-sales-purchase)
1. [Purchase](https://erpnext.com/open-source-sales-purchase)
1. [HRMS](https://erpnext.com/open-source-hrms)
1. [Project Management](https://erpnext.com/open-source-projects)
1. [Support](https://erpnext.com/open-source-help-desk-software)
1. [Asset Management](https://erpnext.com/open-source-asset-management-software)
1. [Quality Management](https://erpnext.com/docs/user/manual/en/quality-management)
1. [Manufacturing](https://erpnext.com/open-source-manufacturing-erp-software)
1. [Website Management](https://erpnext.com/open-source-website-builder-software)
1. [Customize ERPNext](https://erpnext.com/docs/user/manual/en/customize-erpnext)
1. [And More](https://erpnext.com/docs/user/manual/en/)

ERPNext is built on the [Frappe Framework](https://github.com/frappe/frappe), a full-stack web app framework built with Python & JavaScript.

### Manual Install

The Easy Way: our install script for bench will install all dependencies (e.g. MariaDB). See https://github.com/frappe/bench for more details.

New passwords will be created for the ERPNext "Administrator" user, the MariaDB root user, and the frappe user (the script displays the passwords and saves them to ~/frappe_passwords.txt).

---

```
bench get-app --branch main https://github.com/quanteonlab/erp15
bench --site dev_site_ab install-app erp15

```


---

# Instructions for Frappe-ERPNext Version-15 in Ubuntu 22.04 LTS
A complete Guide to Install Frappe/ERPNext version 15  in Ubuntu 22.04 LTS

### Pre-requisites 

      Python 3.11+
      Node.js 18+
      
      Redis 5                                       (caching and real time updates)
      MariaDB 10.3.x / Postgres 9.5.x               (to run database driven apps)
      yarn 1.12+                                    (js dependency manager)
      pip 20+                                       (py dependency manager)
      wkhtmltopdf (version 0.12.5 with patched qt)  (for pdf generation)
      cron                                          (bench's scheduled jobs: automated certificate renewal, scheduled backups)
      NGINX                                         (proxying multitenant sites in production)



### STEP 1 Install git
    sudo apt-get install git

### STEP 2 install python-dev

    sudo apt-get install python3-dev

### STEP 3 Install setuptools and pip (Python's Package Manager).

    sudo apt-get install python3-setuptools python3-pip

### STEP 4 Install virtualenv
    
    sudo apt install python3.11-venv
    

### STEP 5 Install MariaDB

    sudo apt-get install software-properties-common
    sudo apt install mariadb-server
    sudo mysql_secure_installation
    
    
      In order to log into MariaDB to secure it, we'll need the current
      password for the root user. If you've just installed MariaDB, and
      haven't set the root password yet, you should just press enter here.

      Enter current password for root (enter for none): # PRESS ENTER
      OK, successfully used password, moving on...
      
      
      Switch to unix_socket authentication [Y/n] Y
      Enabled successfully!
      Reloading privilege tables..
       ... Success!
 
      Change the root password? [Y/n] Y
      New password: 
      Re-enter new password: 
      Password updated successfully!
      Reloading privilege tables..
       ... Success!

      Remove anonymous users? [Y/n] Y
       ... Success!
 
       Disallow root login remotely? [Y/n] Y
       ... Success!

       Remove test database and access to it? [Y/n] Y
       - Dropping test database...
       ... Success!
       - Removing privileges on test database...
       ... Success!
 
       Reload privilege tables now? [Y/n] Y
       ... Success!

 
    
    
    
### STEP 6  MySQL database development files

    sudo apt-get install libmysqlclient-dev

### STEP 7 Edit the mariadb configuration ( unicode character encoding )

    sudo nano /etc/mysql/mariadb.conf.d/50-server.cnf

add this to the 50-server.cnf file

    
    [server]
    user = mysql
    pid-file = /run/mysqld/mysqld.pid
    socket = /run/mysqld/mysqld.sock
    basedir = /usr
    datadir = /var/lib/mysql
    tmpdir = /tmp
    lc-messages-dir = /usr/share/mysql
    bind-address = 127.0.0.1
    query_cache_size = 16M
    log_error = /var/log/mysql/error.log
    
    [mysqld]
    innodb-file-format=barracuda
    innodb-file-per-table=1
    innodb-large-prefix=1
    character-set-client-handshake = FALSE
    character-set-server = utf8mb4
    collation-server = utf8mb4_unicode_ci      
     
    [mysql]
    default-character-set = utf8mb4

Now press (Ctrl-X) to exit

    sudo service mysql restart

### STEP 8 install Redis
    
    sudo apt-get install redis-server

### STEP 9 install Node.js 18.X package

    sudo apt install curl 
    curl https://raw.githubusercontent.com/creationix/nvm/master/install.sh | bash
    source ~/.profile
    nvm install 18

### STEP 10  install Yarn

    sudo apt-get install npm

    sudo npm install -g yarn

### STEP 11 install wkhtmltopdf

    sudo apt-get install xvfb libfontconfig wkhtmltopdf
    

### STEP 12 install frappe-bench


    sudo -H pip3 install frappe-bench
    
    bench --version


## STEP 12.1 Setup Database Root password

sudo mysql -u root
ALTER USER 'root'@'localhost' IDENTIFIED BY '1234';
FLUSH PRIVILEGES;
EXIT;
    
### STEP 13 initilise the frappe bench & install frappe latest version 

    bench init frappe-bench --frappe-branch version-15 --python python3.11
    
    cd frappe-bench/
    bench start
    
### STEP 14 create a site in frappe bench 
    
    bench new-site demo.com
    
    bench --site demo.com add-to-hosts

Open url http://demo.com:8000 to login 


### STEP 15 install ERPNext Our Vesion

    
usar la version Nuestra

```
bench get-app --branch main https://github.com/quanteonlab/erp15
bench --site dev_site_ab install-app erpnext
```

Get started

```
bench use dev_site_ab
bench start
```

version official
```        
    bench get-app erpnext --branch version-15
    ###OR
    bench get-app https://github.com/frappe/erpnext --branch version-15

    bench --site demo.com install-app erpnext


    bench start

```
---

MACOS

install brew

```
rm -rf /usr/local/var/mysql
rm -rf /usr/local/etc/my.cnf.d
brew service mariadb@10.11
brew install libmpdclient
brew link --force --overwrite mariadb@10.11

bench init frappe-bench --frappe-branch version-15

```

make sure to create frappe with user/db frappe/frappe123 y tabla 127.0.0.1

```
bench new-site dev_site_ab \
  --db-type mariadb \
  --db-host 127.0.0.1 \
  --mariadb-root-username frappe \
  --mariadb-root-password 'frappe123' \
  --admin-password 'Admin123!' \
  --db-password 'SiteDBPass123!'

```

usar la version actual

```
bench get-app --branch main https://github.com/quanteonlab/erp15
bench --site dev_site_ab install-app erpnext
```

Get started

```
bench use dev_site_ab
bench start
```

user
```
Administrator
Admin123!
```


➜  frappe-bench sudo brew services start mariadb            
Password:
Error: Formula `mariadb` is not installed.
➜  frappe-bench brew services start  mariadb@10.11

==> Successfully started `mariadb@10.11` (label: homebrew.mxcl.mariadb@10.11)
➜  frappe-bench brew services list

Name          Status  User     File
dbus          none             
mariadb       none    root     
mariadb@10.11 started nenewang ~/Library/LaunchAgents/homebrew.mxcl.mariadb@10.11.plist
postgresql@14 none             
postgresql@15 none             
redis         none             
unbound       none 
    


    

