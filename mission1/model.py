import os
import time
import torch
from torch import nn
import torch.optim as optim
from collections import deque
import torch.nn.functional as F
from torch.optim import Optimizer
import torchvision.models as models
from torch.utils.data import DataLoader
from torchvision.models import ResNet18_Weights
from torch.utils.tensorboard import SummaryWriter

def load_model(self_supervised=False, projection_dim=128, pretrained=False, 
               linear_protocal=False, supervised=False, test=False, pthpath=None):
    # 使用Tiny ImageNet数据集进行SimCLR自监督学习
    if self_supervised:
        model = models.resnet18(weights=None)
        in_features = model.fc.in_features
        # 加入SimCLR的projection head
        model.fc = nn.Sequential(
            nn.Linear(in_features, 512),
            nn.ReLU(),
            nn.Linear(512, projection_dim)
        )
        # Linear Protocol
        # 冻结模型所有参数，只训练最后一层分类层
        # 冻结参数在train.py中实现
        if linear_protocal:
            if not pthpath:
                raise ValueError('Please provide the path to the checkpoint.')
            model.load_state_dict(torch.load(pthpath))
            model.fc = nn.Linear(in_features, 100)
    # 使用ImageNet预训练模型进行相同的Linear Protocol
    elif pretrained:
        model = models.resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
        model.fc = nn.Linear(model.fc.in_features, 100)
    # 使用CIFAR-100从零进行监督学习
    elif supervised:
        model = models.resnet18(weights=None)
        model.fc = nn.Linear(model.fc.in_features, 100)
    # 完成后进行模型测试
    # 此时任何方式训练的模型，都已经按照CIFAR进行了模型结构调整
    elif test:
        if not pthpath:
            raise ValueError('Please provide the path to the checkpoint.')
        model = models.resnet18()
        model.fc = nn.Linear(model.fc.in_features, 100)
        model.load_state_dict(torch.load(pthpath))
    else:
        raise ValueError('Invalid model type. Please specify one of self_supervised, pretrained, supervised.') 
    return model

# 定义对比损失函数（待选方案1）
# 效果不行
class NTXentLoss1(nn.Module):
    def __init__(self, temperature=0.5):
        super(NTXentLoss1, self).__init__()
        self.temperature = temperature
        self.cosine_similarity = nn.CosineSimilarity(dim=-1)

    def forward(self, features):
        n = features.shape[0]
        labels = torch.cat([torch.arange(n // 2) for _ in range(2)], dim=0)
        labels = (labels.unsqueeze(0) == labels.unsqueeze(1)).float()
        labels = labels.to(features.device)

        features = nn.functional.normalize(features, dim=1)
        similarity_matrix = self.cosine_similarity(features.unsqueeze(1), features.unsqueeze(0)) / self.temperature
        exp_sim = torch.exp(similarity_matrix) * (1 - torch.eye(n, device=features.device))
        sum_exp_sim = torch.sum(exp_sim, dim=1)

        # 加入1e-8防止log(0)的情况。在不加的时候，loss会是inf
        # 加在分母上时，加到1e10，都还是inf，没法用
        # 加在分数外时，加1e-8，是正常数字。
        log_prob = torch.log(exp_sim / sum_exp_sim.unsqueeze(1) + 1e-8)
        mean_log_prob_pos = (labels * log_prob).sum(dim=1) / labels.sum(dim=1)

        # 设置为loss的加和，而不是均值。然后再在跑完一轮epoch之后，进行均值计算
        loss = -mean_log_prob_pos.sum()
        return loss

# 定义对比损失函数（待选方案2）
# 效果也是勉强
class ContrastiveLoss(nn.Module):
    def __init__(self, temperature=1.0):
        super(ContrastiveLoss, self).__init__()
        self.temperature = temperature

    def forward(self, out1, out2):
        batch_size = out1.shape[0]  # 批次大小为64
        # 进行模长归一化，它实为余弦相似度计算
        out1 = F.normalize(out1, p=2, dim=-1)
        out2 = F.normalize(out2, p=2, dim=-1)

        # 按列复制
        labels = torch.eye(batch_size).to(out1.device).repeat(1, 2)

        # 计算out1自身的相似度
        logits_aa = torch.matmul(out1, out1.T) / self.temperature
        # 计算Out2自身的相似度
        logits_bb = torch.matmul(out2, out2.T) / self.temperature
        # 计算out1与out1正向的相似度
        logits_ab = torch.matmul(out1, out2.T) / self.temperature
        # 计算out1与out1反向的相似度
        logits_ba = torch.matmul(out2, out1.T) / self.temperature

        loss_a = F.cross_entropy(torch.cat([logits_ab, logits_aa], dim=1), labels)
        loss_b = F.cross_entropy(torch.cat([logits_ba, logits_bb], dim=1), labels)
        # 平均两个对称的交叉熵损失
        total_loss = (loss_a + loss_b) / 2

        return total_loss

# 定义损失函数（待选方案3）
class NTXentLoss2(nn.Module):
    def __init__(self):
        super(NTXentLoss2, self).__init__()
        self.cosine_similarity = nn.CosineSimilarity(dim=-1)
    
    def forward(self, out1, out2):
        batch_size = out1.shape[0]  # 批次大小为64
        # 进行模长归一化，它实为余弦相似度计算
        out1 = F.normalize(out1, p=2, dim=-1)
        out2 = F.normalize(out2, p=2, dim=-1)

        # 计算out1自身的相似度
        logits_aa = torch.matmul(out1, out1.T)
        # 计算Out2自身的相似度
        logits_bb = torch.matmul(out2, out2.T)
        # 计算out1与out1正向的相似度
        logits_ab = torch.matmul(out1, out2.T)
        # 计算out1与out1反向的相似度
        logits_ba = torch.matmul(out2, out1.T)    

        # 提取矩阵 ab 的对角线元素
        ab_diagonal = logits_ab.diag().unsqueeze(1)  # Shape: [batch_size, 1]
        # 提取矩阵 ab 的非对角线元素
        ab_off_diagonal = logits_ab.masked_fill(torch.eye(batch_size).bool().to(logits_ab.device), float('-inf'))
        # 提取矩阵 aa 的非对角线元素
        aa_off_diagonal = logits_aa.masked_fill(torch.eye(batch_size).bool().to(logits_aa.device), float('-inf'))
        # 合并对角线元素和两个矩阵的非对角线元素
        combined1 = torch.cat([ab_diagonal, ab_off_diagonal, aa_off_diagonal], dim=1)
        # 对合并后的矩阵应用 softmax
        softmax_results1 = F.softmax(combined1, dim=1)
        # 结果中，每行的第一个元素就是对应于 ab 对角线元素的 softmax 结果
        softmax_diagonal1 = softmax_results1[:, 0]
        # 取对数并取负
        log_softmax_diagonal1 = -torch.log(softmax_diagonal1)

        # 提取矩阵 ba 的对角线元素
        ba_diagonal = logits_ba.diag().unsqueeze(1)  # Shape: [batch_size, 1]
        # 提取矩阵 ba 的非对角线元素
        ba_off_diagonal = logits_ba.masked_fill(torch.eye(batch_size).bool().to(logits_ba.device), float('-inf'))
        # 提取矩阵 bb 的非对角线元素
        bb_off_diagonal = logits_bb.masked_fill(torch.eye(batch_size).bool().to(logits_bb.device), float('-inf'))
        # 合并对角线元素和两个矩阵的非对角线元素
        combined2 = torch.cat([ba_diagonal, ba_off_diagonal, bb_off_diagonal], dim=1)
        # 对合并后的矩阵应用 softmax
        softmax_results2 = F.softmax(combined2, dim=1)
        # 结果中，每行的第一个元素就是对应于 ab 对角线元素的 softmax 结果
        softmax_diagonal2 = softmax_results2[:, 0]
        # 取对数并取负
        log_softmax_diagonal2 = -torch.log(softmax_diagonal2)

        log_softmax = sum(log_softmax_diagonal1) + sum(log_softmax_diagonal2)
        log_softmax /= 2*batch_size

        return log_softmax

# 自监督学习训练函数
def self_supervised_train(model: nn.Module, data_loader: DataLoader, optimizer: Optimizer, 
                          criterion: nn.Module, epochs: int = 70, gamma: float = 0.1,
                          logdir: str ='/mnt/ly/models/FinalTerm/mission2/tensorboard/1',
                          save_dir: str ='/mnt/ly/models/FinalTerm/mission2/modelpth/1'):
    
    divided = epochs // 10
    device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
    model.to(device)
    
    writer = SummaryWriter(log_dir=logdir)

    # 添加模型图
    init_img = torch.zeros((1, 3, 224, 224)).to(device)  # 假设输入图像尺寸为 (3, 224, 224)
    writer.add_graph(model, init_img)

    history_loss = deque() # 记录历史训练loss，用于判断是否需要减小学习率
    lowest_loss = float("inf") # 记录最低的训练loss
    lowest_TrainLoss_files = deque() # 记录历史最低训练loss对应的模型文件，用于删除模型文件
    for epoch in range(epochs):
        model.train()
        running_loss = 0.0

        train_start_time = time.time()
        for (images1, images2), _ in data_loader:
            images1 = images1.to(device)
            images2 = images2.to(device)
            optimizer.zero_grad()

            features1 = model(images1)
            features2 = model(images2)

            loss = criterion(features1, features2) 

            # # -----测试-----
            # print(loss)
            # break           
            # # -----测试-----

            loss.backward()
            optimizer.step()
            running_loss += loss.item()

        # # -----测试-----
        # break
        # # -----测试-----

        epoch_loss = running_loss / len(data_loader)
        if isinstance(optimizer, optim.SGD):
            history_loss.append(epoch_loss)

        # 结束训练计时
        train_end_time = time.time()
        train_elapsed_time = train_end_time - train_start_time
        print(f'Epoch {epoch+1}/{epochs}, \nTrain Loss: {epoch_loss:.4f}, Training Time: {train_elapsed_time:.2f}s')

        # 将训练loss写入TensorBoard
        writer.add_scalar('Loss/Train Loss', epoch_loss, epoch)
        writer.add_scalar('Time/Train', train_elapsed_time, epoch)

        # 将当前学习率写入TensorBoard
        lr_i = 1
        for param_group in optimizer.param_groups:
            current_lr = param_group['lr']
            writer.add_scalar(f'Learning Rate/{lr_i}', current_lr, epoch)
            lr_i += 1

        # 保存loss最低的模型
        if epoch_loss < lowest_loss:
            # 更新最小的loss
            lowest_loss = epoch_loss
            # 与按epoch等分进行区分开
            if (epoch+1) % divided != 0:
                file_path = f"{epoch+1}_{epoch_loss}.pth"
                torch.save(model.state_dict(), os.path.join(save_dir, file_path))
                lowest_TrainLoss_files.append(file_path)
                if len(lowest_TrainLoss_files) > 10:
                    file_to_remove = lowest_TrainLoss_files.popleft()
                    os.remove(os.path.join(save_dir, file_to_remove))
        # 把epoch分成十等分，按照epoch进行保存模型，提供更多的模型选择
        if (epoch+1) % divided == 0:
            file_path = f"{epoch+1}_{epoch_loss}.pth"
            torch.save(model.state_dict(), os.path.join(save_dir, file_path))

        if isinstance(optimizer, optim.SGD):
            # 维护准确率历史记录长度
            if len(history_loss) == 150:
                max_acc = max(history_loss)
                min_acc = min(history_loss)
                
                # 检测准确率变化是否小于阈值
                if max_acc - min_acc < 0.005:
                    # 减少学习率
                    for param_group in optimizer.param_groups:
                        param_group['lr'] *= gamma
                    # 清空历史记录以重新开始收集数据
                    history_loss.clear()
                else:
                    # 移除最旧的记录，继续收集直到deque满
                    history_loss.popleft()

    writer.flush()
    writer.close()

# 监督学习训练函数
def supervised_train(model: nn.Module, train_loader: DataLoader, test_loader: DataLoader,
                     optimizer: Optimizer, criterion: nn.Module, epochs: int = 70, 
                     logdir: str ='/mnt/ly/models/FinalTerm/mission2/tensorboard/1',
                     save_dir: str ='/mnt/ly/models/FinalTerm/mission2/modelpth/1',
                     milestones: list = [], gamma: float = 0.1):
    
    device = torch.device("cuda:1" if torch.cuda.is_available() else "cpu")
    model.to(device)
    
    writer = SummaryWriter(log_dir=logdir)

    # 添加模型图
    init_img = torch.zeros((1, 3, 224, 224)).to(device)  # 假设输入图像尺寸为 (3, 224, 224)
    writer.add_graph(model, init_img)

    history_accuracy = deque() # 记录历史测试准确率，用于判断是否需要减小学习率
    best_test_acc = 0.0 # 记录最佳测试准确率
    best_test_files = deque() # 记录历史最佳测试准确率对应的模型文件，用于删除模型文件
    for epoch in range(epochs):
        model.train()
        running_loss = 0.0
        corrects = 0

        # 当optimizer为SGD时，更新学习率。
        # 当optimizer为Adam时，milestones为空，不会执行。
        if epoch+1 in milestones:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= gamma

        train_start_time = time.time()
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            running_loss += loss.item() * images.size(0)
            _, preds = torch.max(outputs, 1)
            corrects += torch.sum(preds == labels.data)

        epoch_loss = running_loss / len(train_loader.dataset)
        epoch_acc = corrects.double() / len(train_loader.dataset)
        history_accuracy.append(epoch_acc)

        # 结束训练计时
        train_end_time = time.time()
        train_elapsed_time = train_end_time - train_start_time
        print(f'Epoch {epoch+1}/{epochs}, \nTrain Loss: {epoch_loss:.4f}, Training Accuracy: {epoch_acc:.4f}, Training Time: {train_elapsed_time:.2f}s')

        # 将训练loss写入TensorBoard
        writer.add_scalar('Loss/Train Loss', epoch_loss, epoch)
        writer.add_scalar('Time/Train', train_elapsed_time, epoch)
        writer.add_scalar('Accuracy/Train Accuracy', epoch_acc, epoch)

        # 将当前学习率写入TensorBoard
        lr_i = 1
        for param_group in optimizer.param_groups:
            current_lr = param_group['lr']
            writer.add_scalar(f'Learning Rate/{lr_i}', current_lr, epoch)
            lr_i += 1

        # 验证步骤
        model.eval()
        test_loss = 0.0
        corrects = 0

        # 开始验证计时
        test_start_time = time.time()

        with torch.no_grad():
            for inputs, labels in test_loader:
                inputs, labels = inputs.to(device), labels.to(device)
                outputs = model(inputs)
                loss = criterion(outputs, labels)
                test_loss += loss.item() * inputs.size(0)
                _, preds = torch.max(outputs, 1)
                corrects += torch.sum(preds == labels.data)

        test_end_time = time.time()
        test_elapsed_time = test_end_time - test_start_time

        test_loss = test_loss / len(test_loader.dataset)
        test_acc = corrects.double() / len(test_loader.dataset)
        print(f'Test Loss: {test_loss:.4f}, Test Accuracy: {test_acc:.4f}, Test Time: {test_elapsed_time:.2f}s')

        # 将验证loss和accuracy写入TensorBoard
        writer.add_scalar('Loss/Test Loss', test_loss, epoch)
        writer.add_scalar('Time/Test', test_elapsed_time, epoch)
        writer.add_scalar('Accuracy/Test Accuracy', test_acc, epoch)

        # 保存最佳模型
        if test_acc > best_test_acc:
            best_test_acc = test_acc
            file_path = f"{epoch+1}_{best_test_acc}.pth"
            torch.save(model.state_dict(), os.path.join(save_dir, file_path))
            best_test_files.append(file_path)
            if len(best_test_files) > 10:
                file_to_remove = best_test_files.popleft()
                os.remove(os.path.join(save_dir, file_to_remove))
    
    writer.flush()
    writer.close()