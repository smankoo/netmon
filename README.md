# netmon
A tool for monitoring internet connection quality

Option 1 (simplest): Run as a vagrant box

    # Install Pre-Requisites
    sudo apt-get update && sudo apt-get install -y linux-headers-`uname -r` virtualbox-dkms virtualbox vagrant git
    
    # Clone the Repository
    git clone https://github.com/smankoo/netmon netmon
    
    # Start the Box
    cd netmon && vagrant up

Option 2 (lightest): Run as a docker container (Ensure that you have docker installed and running)

    docker run --name netmon -i -t -d -v /netmon-logs:/var/log/netmon-logs smankoo/netmon

# Developer Notes

Run the following command to build a new Docker image

    docker build -t smankoo/newcron .

Helpful Aliases

    alias b='docker build -t smankoo/newcron .'
    alias r='docker stop newcron;docker rm newcron;docker run --name newcron -it -d smankoo/newcron'
    alias e='docker exec -it newcron bash'
    alias x='b;r;e'
