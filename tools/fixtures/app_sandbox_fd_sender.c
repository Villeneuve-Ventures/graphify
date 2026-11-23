/* Standalone synthetic-file sender. No forks, process discovery, or graph access. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static int descriptor(const char *text) {
    char *end = NULL;
    errno = 0;
    long value = strtol(text, &end, 10);
    if (errno || !end || *end || value < 3 || value > INT_MAX) return -1;
    return (int)value;
}

int main(int argc, char **argv) {
    if (argc != 5 || getuid() == 0 || getuid() != geteuid()) return 64;
    int channel = descriptor(argv[1]), gate = descriptor(argv[2]);
    if (channel < 0 || gate < 0 || channel == gate) return 64;
    int fd = open(argv[3], O_RDWR | O_NOFOLLOW | O_CLOEXEC);
    int allowed_errno = fd < 0 ? errno : 0;
    struct stat identity;
    memset(&identity, 0, sizeof(identity));
    int allowed_write = 0;
    if (fd >= 0 && fstat(fd, &identity) == 0 && S_ISREG(identity.st_mode)) {
        const char marker[] = "SENDER-READY";
        allowed_write = pwrite(fd, marker, sizeof(marker) - 1, 64) == (ssize_t)(sizeof(marker) - 1);
        if (!allowed_write) allowed_errno = errno;
    }
    int outside = open(argv[4], O_WRONLY | O_NOFOLLOW | O_CLOEXEC);
    int outside_errno = outside < 0 ? errno : 0;
    if (outside >= 0) close(outside);
    printf("{\"event\":\"sender_ready\",\"pid\":%ld,\"ppid\":%ld,\"pgid\":%ld,"
           "\"allowed_write\":%s,\"allowed_errno\":%d,\"outside_errno\":%d,"
           "\"device\":%llu,\"inode\":%llu}\n",
           (long)getpid(), (long)getppid(), (long)getpgrp(),
           allowed_write ? "true" : "false", allowed_errno, outside_errno,
           (unsigned long long)identity.st_dev, (unsigned long long)identity.st_ino);
    fflush(stdout);
    char command = 0;
    if (read(gate, &command, 1) != 1 || command != 'G' || !allowed_write) return 65;
    close(gate);
    char payload = 'F';
    struct iovec vector = { .iov_base = &payload, .iov_len = 1 };
    union { struct cmsghdr align; char bytes[CMSG_SPACE(sizeof(int))]; } control;
    memset(&control, 0, sizeof(control));
    struct msghdr message;
    memset(&message, 0, sizeof(message));
    message.msg_iov = &vector;
    message.msg_iovlen = 1;
    message.msg_control = control.bytes;
    message.msg_controllen = sizeof(control.bytes);
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(fd));
    memcpy(CMSG_DATA(header), &fd, sizeof(fd));
    ssize_t sent = sendmsg(channel, &message, 0);
    int send_errno = sent == 1 ? 0 : errno;
    if (sent != 1) {
        /* A data-only marker permits a denied FD send to finish without a hang. */
        (void)send(channel, "N", 1, 0);
    }
    close(fd);
    printf("{\"event\":\"sender_sent\",\"sent\":%s,\"errno\":%d}\n",
           sent == 1 ? "true" : "false", send_errno);
    fflush(stdout);
    for (;;) pause();
}
