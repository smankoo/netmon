# netmon
A tool for monitoring internet connection quality

Run the following command to build a new Docker image

    docker build -t smankoo/newcron .

Use the following command to run the container

    docker run \
      --name netmon
      -it -d \
      -v ~/testlogs:/var/log/testlogs \
      smankoo/netmon



  # Developer Notes

  Helpful Aliases

    alias b='docker build -t smankoo/newcron .'
    alias r='docker stop newcron;docker rm newcron;docker run --name newcron -it -d smankoo/newcron'
    alias e='docker exec -it newcron bash'
    alias x='b;r;e'
